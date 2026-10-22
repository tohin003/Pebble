/*
 * pebble-vault: Pebble's Keychain vault helper (Phase 22, k22-identity; docs/HARNESS_SIGNING_IDENTITY.md).
 *
 * Why it exists. macOS ties a Keychain item to the code that may read it without asking: its designated
 * requirement and, since macOS 10.12, its partition ID. Code without an Apple-issued Team ID (Pebble's self-signed
 * local identity) has a partition ID derived from its code hash, which changes with every build. This helper is
 * built once by scripts/build_vault_helper.py and copied byte-identical into every release, so its code hash, and
 * with it the partition of the one item it owns, never changes: an "Always Allow" given once stays given.
 *
 * Protocol. One request on stdin and one reply on stdout, each a single line of JSON in exactly these forms (the
 * only client is backend/pebble/vault.py, which writes them byte for byte; any other text is a bad request):
 *   {"op":"ping"}                      -> {"ok":true,"version":1}
 *   {"op":"get_all"}                   -> {"ok":true,"found":true,"map_b64":"<base64>"} | {"ok":true,"found":false}
 *   {"op":"put_all","map_b64":"<b64>"} -> {"ok":true}  (the item's data is replaced, read back and compared)
 * The map is opaque bytes here (vault.py's JSON); it is carried as base64 so this program needs no JSON parser.
 * Errors: {"ok":false,"error":"<code>"} with an OSStatus as "status" for Keychain failures. Item data never
 * appears in an error, and nothing is written to stderr.
 * Exit codes: 0 served, 1 Keychain or internal failure, 2 bad request, 3 caller refused.
 *
 * The item: generic password, service com.pebble.local.vault, account v1, in the default (login) keychain.
 *
 * Caller check. The helper serves only when its parent process is Pebble's own backend inside an intact Pebble.app:
 *   1. the parent satisfies, running,
 *        (identifier "com.pebble.local.backend" or identifier "com.pebble.local") and certificate leaf = H"<sha1>"
 *      where <sha1> is the SHA-1 of the leaf certificate this helper is itself signed with (read with
 *      SecCodeCopySelf), and its leaf certificate has the same SHA-256 as this helper's (Phase 24, V1-140: the
 *      requirement language's H"" is SHA-1, so the SHA-256 is compared here);
 *   2. the parent's executable, by the kernel's path for its PID (proc_pidpath), is
 *      <bundle>.app/Contents/Resources/resources/backend/pebble-backend (Phase 24, V1-025);
 *   3. that enclosing <bundle>.app passes static strict validation with its resources sealed (SecStaticCode, no
 *      kSecCSDoNotValidateResources) against identifier "com.pebble.local" and the same leaf, SHA-1 and SHA-256.
 * The backend's Python payload (_internal/base_library.zip and the rest) is not covered by the backend executable's
 * own signature but is sealed as resources of the app, so a copied backend folder (no enclosing app) or a copied app
 * with an altered payload is refused. This is an interim check: a payload that restores the sealed bytes before it
 * spawns the helper is not detected, so the durable design (after 1.0) lets only the sealed shell call the helper.
 * An unsigned or ad-hoc helper has no certificate and refuses. The parent is found by getppid() and checked with
 * SecCodeCopyGuestWithAttributes and SecCodeCheckValidity; getppid() is read again after every check.
 * PID reuse: a PID names a process only while it lives. If the parent exited between getppid() and the check, its
 * PID could in principle be reused by another process. But a child whose parent exits is re-parented at once, so
 * the second getppid() then differs from the first and the request is refused. The backend spawns this helper per
 * request and waits for it, so the parent is alive throughout in normal operation.
 *
 * Cost: step 3 hashes the app's sealed resources on every request, about what `codesign --verify --strict` of the
 * app costs; ping runs the same check, because the caller check comes before the request is read.
 *
 * Other properties: no environment variable is read; the hardened runtime (from signing) disables DYLD_* injection
 * and enforces library validation; requests are capped at MAX_REQUEST bytes and the map at MAX_MAP bytes; buffers
 * that held item data are cleared before they are freed.
 */
#define __STDC_WANT_LIB_EXT1__ 1 /* memset_s */
#include <CommonCrypto/CommonDigest.h>
#include <CoreFoundation/CoreFoundation.h>
#include <Security/Security.h>
#include <libproc.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#define MAX_MAP (1024 * 1024)
#define MAX_B64 (((MAX_MAP + 2) / 3) * 4)
#define MAX_REQUEST (MAX_B64 + 64)
#define SERVICE "com.pebble.local.vault"
#define ACCOUNT "v1"
#define CALLER_IDENTIFIERS "(identifier \"com.pebble.local.backend\" or identifier \"com.pebble.local\")"
#define APP_IDENTIFIER "identifier \"com.pebble.local\""
#define BACKEND_IN_APP "/Contents/Resources/resources/backend/pebble-backend"
#define APP_SUFFIX ".app"
#define APP_VALIDATION (kSecCSStrictValidate | kSecCSCheckAllArchitectures) /* resources are validated by default */

enum { EXIT_SERVED = 0, EXIT_FAILED = 1, EXIT_BAD_REQUEST = 2, EXIT_REFUSED = 3 };

static const char PING[] = "{\"op\":\"ping\"}";
static const char GET_ALL[] = "{\"op\":\"get_all\"}";
static const char PUT_PREFIX[] = "{\"op\":\"put_all\",\"map_b64\":\"";
static const char PUT_SUFFIX[] = "\"}";
static const char B64[] = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

static void clear(void *buffer, size_t size) {
    if (buffer != NULL && size > 0) {
        memset_s(buffer, size, 0, size);
    }
}

static int reply(const char *text, int code) {
    fputs(text, stdout);
    fputc('\n', stdout);
    fflush(stdout);
    return code;
}

static int fail(const char *error, OSStatus status, int code) {
    char text[160];
    if (status != 0) {
        snprintf(text, sizeof text, "{\"ok\":false,\"error\":\"%s\",\"status\":%d}", error, (int)status);
    } else {
        snprintf(text, sizeof text, "{\"ok\":false,\"error\":\"%s\"}", error);
    }
    return reply(text, code);
}

/* ---- Caller check -------------------------------------------------------------------------------------------- */

/* A signer's leaf certificate: its SHA-1 as hex (the requirement language's H"" hash) and its SHA-256. */
typedef struct {
    char sha1[CC_SHA1_DIGEST_LENGTH * 2 + 1];
    unsigned char sha256[CC_SHA256_DIGEST_LENGTH];
} Leaf;

/* The leaf of a signed code object (static, or the static code under a running guest); 0 when it has none. */
static int leaf_of(SecStaticCodeRef code, Leaf *leaf) {
    CFDictionaryRef info = NULL;
    int found = 0;
    if (SecCodeCopySigningInformation(code, kSecCSSigningInformation, &info) != errSecSuccess || info == NULL) {
        return 0;
    }
    CFArrayRef certificates = CFDictionaryGetValue(info, kSecCodeInfoCertificates);
    if (certificates != NULL && CFGetTypeID(certificates) == CFArrayGetTypeID() && CFArrayGetCount(certificates) >= 1) {
        CFDataRef der = SecCertificateCopyData((SecCertificateRef)CFArrayGetValueAtIndex(certificates, 0));
        if (der != NULL) {
            unsigned char sha1[CC_SHA1_DIGEST_LENGTH];
            CC_SHA1(CFDataGetBytePtr(der), (CC_LONG)CFDataGetLength(der), sha1);
            CC_SHA256(CFDataGetBytePtr(der), (CC_LONG)CFDataGetLength(der), leaf->sha256);
            CFRelease(der);
            for (int i = 0; i < CC_SHA1_DIGEST_LENGTH; i++) {
                snprintf(leaf->sha1 + 2 * i, 3, "%02x", sha1[i]);
            }
            found = 1;
        }
    }
    CFRelease(info); /* unsigned or ad hoc: no certificate, so nothing to compare a caller with */
    return found;
}

static int own_leaf(Leaf *leaf) {
    SecCodeRef self = NULL;
    SecStaticCodeRef code = NULL;
    int found = 0;
    if (SecCodeCopySelf(kSecCSDefaultFlags, &self) == errSecSuccess
        && SecCodeCopyStaticCode(self, kSecCSDefaultFlags, &code) == errSecSuccess) {
        found = leaf_of(code, leaf);
    }
    if (code != NULL) {
        CFRelease(code);
    }
    if (self != NULL) {
        CFRelease(self);
    }
    return found;
}

static int same_leaf(const Leaf *a, const Leaf *b) {
    return strcmp(a->sha1, b->sha1) == 0 && memcmp(a->sha256, b->sha256, sizeof a->sha256) == 0;
}

/* "<identifiers> and certificate leaf = H"<sha1>"", or NULL. */
static SecRequirementRef requirement_for(const char *identifiers, const Leaf *leaf) {
    char text[256];
    SecRequirementRef requirement = NULL;
    snprintf(text, sizeof text, "%s and certificate leaf = H\"%s\"", identifiers, leaf->sha1);
    CFStringRef source = CFStringCreateWithCString(NULL, text, kCFStringEncodingUTF8);
    if (source != NULL) {
        if (SecRequirementCreateWithString(source, kSecCSDefaultFlags, &requirement) != errSecSuccess) {
            requirement = NULL;
        }
        CFRelease(source);
    }
    return requirement;
}

/* V1-025: the app enclosing the parent's executable, from the kernel's path for the parent's PID. The parent must be
 * <bundle>.app/Contents/Resources/resources/backend/pebble-backend; bundle receives "<bundle>.app". */
static int parent_app(pid_t parent, char bundle[PROC_PIDPATHINFO_MAXSIZE]) {
    char path[PROC_PIDPATHINFO_MAXSIZE];
    size_t suffix = sizeof BACKEND_IN_APP - 1;
    size_t extension = sizeof APP_SUFFIX - 1;
    memset(path, 0, sizeof path);
    int written = proc_pidpath(parent, path, sizeof path);
    if (written <= 0) {
        return 0;
    }
    size_t length = strnlen(path, sizeof path);
    if (length >= sizeof path || length <= suffix + extension || path[0] != '/') {
        return 0;
    }
    size_t app = length - suffix;
    if (strcmp(path + app, BACKEND_IN_APP) != 0 || memcmp(path + app - extension, APP_SUFFIX, extension) != 0
        || path[app - extension - 1] == '/') {
        return 0;
    }
    memcpy(bundle, path, app);
    bundle[app] = '\0';
    return 1;
}

/* V1-025: the enclosing app, validated statically and strictly with its sealed resources, must be com.pebble.local
 * signed with this helper's own leaf (SHA-1 in the requirement, SHA-256 compared here). */
static int app_intact(const char *bundle, const Leaf *own) {
    int intact = 0;
    Leaf leaf;
    SecStaticCodeRef app = NULL;
    SecRequirementRef requirement = requirement_for(APP_IDENTIFIER, own);
    CFURLRef url = CFURLCreateFromFileSystemRepresentation(NULL, (const UInt8 *)bundle, (CFIndex)strlen(bundle), true);
    if (requirement != NULL && url != NULL && SecStaticCodeCreateWithPath(url, kSecCSDefaultFlags, &app) == errSecSuccess
        && SecStaticCodeCheckValidity(app, APP_VALIDATION, requirement) == errSecSuccess && leaf_of(app, &leaf)) {
        intact = same_leaf(&leaf, own);
    }
    if (app != NULL) {
        CFRelease(app);
    }
    if (url != NULL) {
        CFRelease(url);
    }
    if (requirement != NULL) {
        CFRelease(requirement);
    }
    return intact;
}

static int caller_allowed(void) {
    pid_t parent = getppid();
    Leaf own;
    Leaf caller;
    char bundle[PROC_PIDPATHINFO_MAXSIZE];
    int allowed = 0;
    SecRequirementRef requirement = NULL;
    CFNumberRef pid = NULL;
    CFDictionaryRef attributes = NULL;
    SecCodeRef guest = NULL;
    if (parent <= 1 || !own_leaf(&own) || !parent_app(parent, bundle)) {
        return 0;
    }
    requirement = requirement_for(CALLER_IDENTIFIERS, &own);
    int value = (int)parent;
    pid = CFNumberCreate(NULL, kCFNumberIntType, &value);
    const void *keys[] = {kSecGuestAttributePid};
    const void *values[] = {pid};
    attributes = CFDictionaryCreate(NULL, keys, values, 1, &kCFTypeDictionaryKeyCallBacks,
                                    &kCFTypeDictionaryValueCallBacks);
    if (requirement == NULL || pid == NULL || attributes == NULL
        || SecCodeCopyGuestWithAttributes(NULL, attributes, kSecCSDefaultFlags, &guest) != errSecSuccess) {
        goto done;
    }
    /* Cheap checks first; the app's resource seal last; the parent must still be the same process after all of them. */
    allowed = SecCodeCheckValidity(guest, kSecCSDefaultFlags, requirement) == errSecSuccess && leaf_of(guest, &caller)
              && same_leaf(&caller, &own) && app_intact(bundle, &own) && getppid() == parent;
done:
    if (guest != NULL) {
        CFRelease(guest);
    }
    if (attributes != NULL) {
        CFRelease(attributes);
    }
    if (pid != NULL) {
        CFRelease(pid);
    }
    if (requirement != NULL) {
        CFRelease(requirement);
    }
    return allowed;
}

/* ---- Base64 (strict) ------------------------------------------------------------------------------------------ */

static int b64_value(unsigned char c) {
    const char *at = memchr(B64, c, 64);
    return (c != 0 && at != NULL) ? (int)(at - B64) : -1;
}

/* Decodes text[0..length) into out (capacity length / 4 * 3); returns the byte count, or -1 if malformed. */
static long b64_decode(const char *text, size_t length, unsigned char *out) {
    if (length % 4 != 0) {
        return -1;
    }
    size_t written = 0;
    for (size_t i = 0; i < length; i += 4) {
        int v[4];
        int padding = 0;
        for (int j = 0; j < 4; j++) {
            unsigned char c = (unsigned char)text[i + j];
            if (c == '=') {
                if (i + 4 != length || j < 2) {
                    return -1;
                }
                padding++;
                v[j] = 0;
            } else {
                if (padding > 0) {
                    return -1;
                }
                v[j] = b64_value(c);
                if (v[j] < 0) {
                    return -1;
                }
            }
        }
        unsigned long triple = ((unsigned long)v[0] << 18) | ((unsigned long)v[1] << 12) | ((unsigned long)v[2] << 6)
                               | (unsigned long)v[3];
        out[written++] = (unsigned char)(triple >> 16);
        if (padding < 2) {
            out[written++] = (unsigned char)(triple >> 8);
        }
        if (padding < 1) {
            out[written++] = (unsigned char)triple;
        }
    }
    return (long)written;
}

static void b64_write(const unsigned char *data, size_t length) {
    char block[4];
    for (size_t i = 0; i < length; i += 3) {
        unsigned long triple = (unsigned long)data[i] << 16;
        if (i + 1 < length) {
            triple |= (unsigned long)data[i + 1] << 8;
        }
        if (i + 2 < length) {
            triple |= data[i + 2];
        }
        block[0] = B64[(triple >> 18) & 63];
        block[1] = B64[(triple >> 12) & 63];
        block[2] = i + 1 < length ? B64[(triple >> 6) & 63] : '=';
        block[3] = i + 2 < length ? B64[triple & 63] : '=';
        fwrite(block, 1, 4, stdout);
    }
}

/* ---- The one item --------------------------------------------------------------------------------------------- */

static CFMutableDictionaryRef item_query(void) {
    CFMutableDictionaryRef query = CFDictionaryCreateMutable(NULL, 0, &kCFTypeDictionaryKeyCallBacks,
                                                             &kCFTypeDictionaryValueCallBacks);
    CFStringRef service = CFStringCreateWithCString(NULL, SERVICE, kCFStringEncodingUTF8);
    CFStringRef account = CFStringCreateWithCString(NULL, ACCOUNT, kCFStringEncodingUTF8);
    if (query == NULL || service == NULL || account == NULL) {
        if (query != NULL) {
            CFRelease(query);
        }
        query = NULL;
    } else {
        CFDictionarySetValue(query, kSecClass, kSecClassGenericPassword);
        CFDictionarySetValue(query, kSecAttrService, service);
        CFDictionarySetValue(query, kSecAttrAccount, account);
    }
    if (service != NULL) {
        CFRelease(service);
    }
    if (account != NULL) {
        CFRelease(account);
    }
    return query;
}

/* errSecSuccess with *data set (the caller clears and releases it), errSecItemNotFound, or another status. */
static OSStatus item_read(CFDataRef *data) {
    CFMutableDictionaryRef query = item_query();
    if (query == NULL) {
        return errSecAllocate;
    }
    CFDictionarySetValue(query, kSecReturnData, kCFBooleanTrue);
    CFDictionarySetValue(query, kSecMatchLimit, kSecMatchLimitOne);
    CFTypeRef found = NULL;
    OSStatus status = SecItemCopyMatching(query, &found);
    CFRelease(query);
    if (status == errSecSuccess && (found == NULL || CFGetTypeID(found) != CFDataGetTypeID())) {
        if (found != NULL) {
            CFRelease(found);
        }
        return errSecInternalComponent;
    }
    *data = (CFDataRef)found;
    return status;
}

static OSStatus item_write(const unsigned char *bytes, size_t length) {
    CFMutableDictionaryRef query = item_query();
    CFDataRef data = CFDataCreate(NULL, bytes, (CFIndex)length);
    CFMutableDictionaryRef change = CFDictionaryCreateMutable(NULL, 0, &kCFTypeDictionaryKeyCallBacks,
                                                              &kCFTypeDictionaryValueCallBacks);
    OSStatus status = errSecAllocate;
    if (query != NULL && data != NULL && change != NULL) {
        CFDictionarySetValue(change, kSecValueData, data);
        status = SecItemUpdate(query, change);
        if (status == errSecItemNotFound) {
            CFDictionarySetValue(query, kSecValueData, data);
            CFDictionarySetValue(query, kSecAttrLabel, CFSTR("Pebble vault"));
            CFDictionarySetValue(query, kSecAttrComment, CFSTR("Pebble's provider secrets, kept by its vault helper."));
            status = SecItemAdd(query, NULL);
        }
    }
    if (change != NULL) {
        CFRelease(change);
    }
    if (data != NULL) {
        CFRelease(data); /* CFData copied the bytes; the caller clears its own buffer */
    }
    if (query != NULL) {
        CFRelease(query);
    }
    return status;
}

static int serve_get_all(void) {
    CFDataRef data = NULL;
    OSStatus status = item_read(&data);
    if (status == errSecItemNotFound) {
        return reply("{\"ok\":true,\"found\":false}", EXIT_SERVED);
    }
    if (status != errSecSuccess) {
        return fail("keychain_read_failed", status, EXIT_FAILED);
    }
    CFIndex length = CFDataGetLength(data);
    if (length > MAX_MAP) {
        CFRelease(data);
        return fail("item_too_large", 0, EXIT_FAILED);
    }
    fputs("{\"ok\":true,\"found\":true,\"map_b64\":\"", stdout);
    b64_write(CFDataGetBytePtr(data), (size_t)length);
    CFRelease(data);
    return reply("\"}", EXIT_SERVED);
}

static int serve_put_all(const char *text, size_t length) {
    if (length == 0 || length > MAX_B64) {
        return fail("bad_map", 0, EXIT_BAD_REQUEST);
    }
    unsigned char *map = malloc(length / 4 * 3);
    if (map == NULL) {
        return fail("internal", 0, EXIT_FAILED);
    }
    long size = b64_decode(text, length, map);
    int code;
    if (size <= 0 || size > MAX_MAP) {
        code = fail("bad_map", 0, EXIT_BAD_REQUEST);
    } else {
        OSStatus status = item_write(map, (size_t)size);
        if (status != errSecSuccess) {
            code = fail("keychain_write_failed", status, EXIT_FAILED);
        } else {
            CFDataRef back = NULL;
            status = item_read(&back);
            int same = status == errSecSuccess && CFDataGetLength(back) == size
                       && memcmp(CFDataGetBytePtr(back), map, (size_t)size) == 0;
            if (back != NULL) {
                CFRelease(back);
            }
            code = same ? reply("{\"ok\":true}", EXIT_SERVED) : fail("verify_failed", status, EXIT_FAILED);
        }
    }
    clear(map, length / 4 * 3);
    free(map);
    return code;
}

int main(void) {
    if (!caller_allowed()) {
        return fail("caller_refused", 0, EXIT_REFUSED);
    }
    char *request = malloc(MAX_REQUEST + 2);
    if (request == NULL) {
        return fail("internal", 0, EXIT_FAILED);
    }
    size_t length = 0;
    size_t got;
    while ((got = fread(request + length, 1, MAX_REQUEST + 1 - length, stdin)) > 0) {
        length += got;
        if (length > MAX_REQUEST) {
            break;
        }
    }
    int code;
    if (length > MAX_REQUEST) {
        code = fail("request_too_large", 0, EXIT_BAD_REQUEST);
    } else {
        if (length > 0 && request[length - 1] == '\n') {
            length--;
        }
        size_t prefix = sizeof PUT_PREFIX - 1;
        size_t suffix = sizeof PUT_SUFFIX - 1;
        if (length == sizeof PING - 1 && memcmp(request, PING, length) == 0) {
            code = reply("{\"ok\":true,\"version\":1}", EXIT_SERVED);
        } else if (length == sizeof GET_ALL - 1 && memcmp(request, GET_ALL, length) == 0) {
            code = serve_get_all();
        } else if (length > prefix + suffix && memcmp(request, PUT_PREFIX, prefix) == 0
                   && memcmp(request + length - suffix, PUT_SUFFIX, suffix) == 0) {
            code = serve_put_all(request + prefix, length - prefix - suffix);
        } else {
            code = fail("bad_request", 0, EXIT_BAD_REQUEST);
        }
    }
    clear(request, MAX_REQUEST + 2);
    free(request);
    return code;
}
