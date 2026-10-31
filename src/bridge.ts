import { invoke, isTauri } from "@tauri-apps/api/core";
import { getCurrentWindow } from "@tauri-apps/api/window";
import { listen } from "@tauri-apps/api/event";
import { open } from "@tauri-apps/plugin-dialog";
export const native = isTauri();
/** Phase 24: a refusal carrying the backend's content-free code (main.py), e.g. "trashed_conversation" or "disk_full". */
export class RpcError extends Error {
  code: string;
  constructor(message: string, code: string) {
    super(message);
    this.name = "RpcError";
    this.code = code;
  }
  toString() {
    return this.message;
  }
}
const CODED = /^([a-z][a-z0-9_]{0,63})\u001f([\s\S]*)$/;
export async function rpc<T = any>(
  method: string,
  params: Record<string, any> = {},
): Promise<T> {
  if (!native)
    throw new Error(
      "Desktop actions are available in the Pebble app. This browser is a visual preview.",
    );
  try {
    return await invoke<T>("rpc", { method, params });
  } catch (cause) {
    const coded = typeof cause === "string" ? CODED.exec(cause) : null;
    throw coded ? new RpcError(coded[2], coded[1]) : cause;
  }
}
/** Phase 24 (V1-112): open an https page in the person's browser through the main window's open_url route
 * (app.open_url also refuses anything but https). Anything else is refused before any call. */
export async function openExternal(url: string): Promise<void> {
  if (typeof url !== "string" || url.length > 3000 || !/^https:\/\/[^\s]+$/i.test(url))
    throw new Error("Only https pages can be opened from here.");
  await rpc("open_url", { url });
}
export async function selectFile(
  directory = false,
  model = false,
  skill = false,
): Promise<string | null> {
  if (!native) throw new Error("Open the desktop app to choose local files.");
  const result = await open({
    directory,
    multiple: false,
    // Phase 45 (F029): the bare picker said nothing about what it chose
    title: directory ? "Choose a project folder" : undefined,
    filters: directory
      ? undefined
      : skill
        ? [{ name: "Markdown skill", extensions: ["md"] }]
        : model
          ? [{ name: "GGUF model", extensions: ["gguf"] }]
          : undefined,
  });
  return typeof result === "string" ? result : null;
}
export async function subscribe(
  callback: (event: { event: string; data: any }) => void,
) {
  return native
    ? listen<{ event: string; data: any }>("backend-event", (event) =>
        callback(event.payload),
      )
    : () => {};
}
export async function setCompact(enabled: boolean) {
  if (native) await invoke("compact", { enabled });
}

export async function selectAttachments(): Promise<string[]> {
  if (!native) throw new Error("Open the desktop app to choose local files.");
  const result = await open({
    multiple: true,
    title: "Attach files",
  });
  return Array.isArray(result)
    ? result
    : typeof result === "string"
      ? [result]
      : [];
}
/** Paste (V1-042): send a pasted file in base64 chunks of at most 96,000 characters (the shell refuses RPC
 * parameters over 128,000), one after another, and return the chip of the final call. */
export const PASTE_CHUNK_BYTES = 72_000;
export async function attachBytes(id: string, name: string, blob: Blob): Promise<any> {
  if (blob.size > 50_000_000) throw new Error("Use a file smaller than 50 MB.");
  const data = new Uint8Array(await blob.arrayBuffer());
  let upload: string | undefined;
  for (let offset = 0; ; offset += PASTE_CHUNK_BYTES) {
    const part = data.subarray(offset, offset + PASTE_CHUNK_BYTES);
    let binary = "";
    for (let i = 0; i < part.length; i += 0x8000) binary += String.fromCharCode(...part.subarray(i, i + 0x8000));
    const final = offset + PASTE_CHUNK_BYTES >= data.length;
    const result = await rpc("attach_bytes", { id, name, data_base64: btoa(binary), upload, offset, final });
    if (final) return result;
    upload = result.upload;
  }
}
export async function subscribeDrops(
  callback: (type: string, paths: string[]) => void,
) {
  return native
    ? getCurrentWindow().onDragDropEvent(({ payload }) =>
        callback(payload.type, "paths" in payload ? payload.paths : []),
      )
    : () => {};
}
