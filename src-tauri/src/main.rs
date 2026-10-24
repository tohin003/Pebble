#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]
use std::{collections::HashMap, io::{BufRead, BufReader, Write}, process::{Child, ChildStdin, Command, Stdio}, sync::{Arc, Mutex, mpsc, atomic::{AtomicBool, AtomicU64, Ordering}}, time::{Duration, Instant}};
use tauri::{Emitter, Manager};
use serde_json::{Value, json};

#[derive(Default)]
struct AllowedViews(Arc<Mutex<std::collections::HashSet<String>>>);

fn host_url(url:&tauri::Url) -> bool {
    (url.scheme()=="tauri" && url.host_str()==Some("localhost")) ||
    (["http","https"].contains(&url.scheme()) && url.host_str()==Some("tauri.localhost")) ||
    (cfg!(debug_assertions) && url.scheme()=="http" && [Some("127.0.0.1"),Some("localhost")].contains(&url.host_str()) && url.port()==Some(1420))
}

struct Backend {
    input: Mutex<Option<ChildStdin>>,
    child: Mutex<Option<Child>>,
    pending: Arc<Mutex<HashMap<String, mpsc::Sender<Result<Value,String>>>>>,
    sequence: AtomicU64,
    restarts: Mutex<(usize, Option<Instant>)>, // attempts and the last attempt: 1 s, 5 s, then 30 s apart
    outbox: Mutex<Option<mpsc::Sender<()>>>,   // wakes the native notification pump
}
/// Backend routes only this shell may call. They are absent from ALLOWED, and rpc() refuses them by name.
const NATIVE_ONLY: &[&str] = &["lifecycle_event","lifecycle_quit_summary","notification_native_pending","notification_native_delivered",
    "export_destination_bind","export_native_ready","export_pdf_attach","export_pdf_failed","telemetry_destination_bind"];
static QUIT_CONFIRMED: AtomicBool = AtomicBool::new(false);

impl Backend {
    fn stop(&self) {
        self.input.lock().unwrap().take(); // EOF lets Python stop its owned model gracefully.
        if let Some(mut child) = self.child.lock().unwrap().take() {
            for _ in 0..50 {
                if child.try_wait().ok().flatten().is_some() { return; }
                std::thread::sleep(Duration::from_millis(100));
            }
            let _ = child.kill(); let _ = child.wait();
        }
    }
    /// One request over the backend pipe; rpc() and the Rust-only callers share it.
    fn call(&self, method:&str, params:Value, timeout:Duration) -> Result<Value,String> {
        let id = self.sequence.fetch_add(1, Ordering::Relaxed).to_string();
        let (sender, receiver) = mpsc::channel();
        self.pending.lock().unwrap().insert(id.clone(), sender);
        let line = format!("{}\n", json!({"id":id,"method":method,"params":params}));
        let written = {
            let mut guard = self.input.lock().unwrap();
            guard.as_mut().ok_or("Local service is offline.".to_owned()).and_then(|input| input.write_all(line.as_bytes()).and_then(|_| input.flush()).map_err(|e| e.to_string()))
        };
        if let Err(error) = written { self.pending.lock().unwrap().remove(&id); return Err(error); }
        match receiver.recv_timeout(timeout) {
            Ok(result) => result,
            Err(_) => { self.pending.lock().unwrap().remove(&id); Err("Local service did not respond. Restart Pebble and try again.".into()) }
        }
    }
    fn alive(&self) -> bool { self.child.lock().unwrap().as_mut().is_some_and(|child| matches!(child.try_wait(), Ok(None))) }
}
/// Reachable only from Rust code paths in this file; never a tauri::command.
fn native_call(backend:&Backend, method:&str, params:Value, timeout:Duration) -> Result<Value,String> {
    if !NATIVE_ONLY.contains(&method) { return Err("Not a native-only call.".into()); }
    // Phase 24: Rust-side callers keep message-only errors (the reader prefixes a backend error code and U+001F).
    backend.call(method, params, timeout).map_err(|e| e.split_once('\u{1f}').map(|(_, message)| message.to_owned()).unwrap_or(e))
}
fn hex32(value:&str) -> bool { value.len()==32 && value.bytes().all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b)) }
fn main_only(window:&tauri::WebviewWindow) -> Result<(),String> { if window.label()=="main" { Ok(()) } else { Err("Open the workspace to change this.".into()) } }
/// Settling a goal is the person's decision, made where its evidence is shown: the companion cannot send these.
fn settles_goal(text:&str) -> bool {
    let mut words = text.trim_start().split_whitespace().map(str::to_lowercase);
    match (words.next().as_deref(), words.next().as_deref()) {
        (Some("/criteria"), Some("check" | "uncheck" | "accept" | "reject")) => true,
        (Some("/goal"), Some("done" | "abandon")) => true,
        _ => false,
    }
}
/// Phase 24 (V1-055): whether `next` grants more than `current`: a higher permission mode, or a tool that is not on
/// now. Anything unreadable counts as wider (fail closed).
fn widens(current:&Value, next:&Value) -> bool {
    let rank = |v:&Value| match v.get("mode").and_then(Value::as_str) { Some("ask")=>Some(0), Some("auto")=>Some(1), Some("full")=>Some(2), _=>None };
    let (Some(before), Some(after)) = (rank(current), rank(next)) else { return true };
    if after > before { return true; }
    let Some(tools) = next.get("tools").and_then(Value::as_object) else { return true };
    tools.iter().any(|(name, on)| match on.as_bool() {
        Some(false) => false,
        Some(true) => current.get("tools").and_then(|t| t.get(name)).and_then(Value::as_bool) != Some(true),
        None => true,
    })
}
/// Phase 24 (V1-055): whether the permissions the pet sends would widen the ones the host holds now (read just before).
fn pet_widens(backend:&Backend, params:&Value) -> Result<bool,String> {
    let current = backend.call("state", json!({}), Duration::from_secs(30))?;
    let held = current.pointer("/settings/permissions").cloned().unwrap_or(Value::Null);
    Ok(widens(&held, params.get("value").unwrap_or(&Value::Null)))
}

#[tauri::command]
async fn rpc(window:tauri::WebviewWindow, backend: tauri::State<'_, Arc<Backend>>, views: tauri::State<'_, AllowedViews>, method:String, params:Value) -> Result<Value,String> {
    const ALLOWED: &[&str] = &["state","new_chat","get_chat","set_workspace","send","cancel","model_load","model_unload","set_model","approve","reject","apps","attach","attach_url","attach_bytes","remove_source","delete_chat","chat_rename","setting","open_app","undo","model_profiles","model_discover","model_save","model_save_many","model_select","model_effort","model_remove","memory_list","memory_save","memory_delete","memory_clear","memory_candidates","memory_maintain","memory_resolve","memory_export","extensions","skill_inspect","hook_events","extension_add","extension_import","extension_toggle","extension_remove","media_artifact","media_download","plugin_package_preview","plugin_package_import","plugin_package_install","plugin_package_update","plugin_package_history","plugin_package_inspect","plugin_package_preview_rollback","plugin_package_rollback","connector_discover_interop","connector_preview_registration","connector_register_client","connector_metadata","connector_discover","connector_preview","connector_start","connector_status","connector_cancel","connector_save_bearer","connector_save_environment","connector_disconnect","terminal_propose","open_url","chatgpt_start","chatgpt_status","chatgpt_cancel","chatgpt_sign_out","view_stage","view_stop","artifact_get","artifact_history","artifact_submit","artifact_stop","artifact_view_open","mods","mod_example","mod_preview","mod_install","mod_toggle","mod_remove","run_list","run_get","run_activity","run_handoff","run_compact","run_context_archive","search_stats","run_events","stream_read","process_list","process_read","process_stop","process_inspect","process_dismiss","run_pause","run_resume","run_held_tests","run_held_finish","run_held_accept","run_reconcile","run_readback","upgrade_backups","upgrade_discard_backup","upgrade_retry","process_terminal","process_input_human","plan_credentials","plan_credential_save","plan_credential_delete","memory_contests","memory_resolve_contest","memory_relations","memory_forget_preview","memory_archive_export_to","memory_archive_preview_file","memory_archive_import_file","model_handoff_preview","model_handoff_approve","model_handoff_start","connector_save_environment_route","connector_send_confirm","research_sources","screen_windows","screen_session_grant","screen_sessions","screen_handoff_done","screen_session_revoke","browser_status","browser_start","browser_stop","browser_tabs","browser_open_tab","browser_profile_forget","command_catalogue","run_steer","question_answer","question_cancel","agent_list","agent_get","agent_steer","agent_cancel","job_list","job_get","job_preview","job_create","job_pause","job_preview_resume","job_resume","job_delete","notification_ack","git_capabilities","git_identity","git_status","git_diff","git_history","git_worktrees","git_inspect","git_preview_create","git_create_worktree","git_preview_remove","git_remove_worktree","git_forget_worktree","git_export","git_export_file","git_plan_propose","git_plan_receipts","git_plan_readback","agent_worktrees","agent_worktree_retire","agent_worktree_reconcile","project_recipes","environment_list","environment_plan","environment_remove","notification_status","notification_mute","goal_list","goal_get","goal_create","goal_state","goal_criterion_add","goal_criterion_edit","goal_criterion_check","goal_criterion_review","goal_step_add","goal_step_edit","goal_step_block","goal_blockers","goal_continue","export_preview","export_render","export_cancel","export_save","export_list","export_get","export_discard","telemetry_get","telemetry_set","telemetry_export","telemetry_clear","artifact_refresh","quiz_attempt","quiz_start","quiz_save","quiz_submit","quiz_share","rewind_preview","rewind_apply","context_inspect","context_compact","browser_extension_status","browser_extension_pause","browser_extension_resume","browser_extension_stop","browser_extension_unpair","storage_summary","storage_reveal","storage_delete","storage_set","storage_settings_resolve","storage_restore_chat","storage_purge_chat","storage_refresh","storage_trash_chat","storage_trash_all","storage_purge_all","storage_forget_resolve","storage_cleanup_legacy","browser_connect_status","browser_connect_install"];
    debug_assert!(ALLOWED.iter().all(|m| !NATIVE_ONLY.contains(m)));
    if !["main","pet"].contains(&window.label()) { return Err("Generated views cannot invoke native agent controls.".into()); }
    // Phase 24 (V1-108, t24-ux-settings): plain words; activity.ts isUnsupported accepts both sentences.
    if NATIVE_ONLY.contains(&method.as_str()) { return Err("Pebble can't do that from this window.".into()); }
    if !ALLOWED.contains(&method.as_str()) || !params.is_object() || params.to_string().len()>128000 { return Err("Pebble can't do that from this window.".into()); }
    if window.label()=="pet" && !["state","new_chat","get_chat","send","cancel","open_url","attach","attach_url","attach_bytes","remove_source","model_select","model_effort","setting","set_workspace","terminal_propose","view_stage","view_stop","run_list","run_get","run_handoff","run_compact","run_context_archive","search_stats","run_events","run_pause","run_resume","run_steer","question_answer","question_cancel","command_catalogue","goal_list","artifact_get","artifact_history","artifact_refresh","quiz_attempt","quiz_start","quiz_save","quiz_submit"].contains(&method.as_str()) { return Err("Open the workspace to manage connections or review actions.".into()); }
    if window.label()=="pet" && method=="setting" && params.get("key").and_then(|x|x.as_str())!=Some("permissions") { return Err("Open the workspace to change this setting.".into()); }
    if window.label()=="pet" && method=="send" && params.get("text").and_then(Value::as_str).is_some_and(settles_goal) { return Err("Open the workspace to check criteria or finish a goal.".into()); }
    // Phase 19: only the shell names the window sharing a quiz result; whatever the webview sent is overwritten.
    // Phase 20: the same for the rewind and context routes, which refuse any surface but "main".
    // Phase 24: the held-task decisions too (refused unless "main").
    let mut params = params;
    if method=="quiz_share" || method=="rewind_preview" || method=="rewind_apply" || method=="context_inspect" || method=="context_compact" || method=="run_held_tests" || method=="run_held_finish" || method=="run_held_accept" { if let Some(map)=params.as_object_mut() { map.insert("surface".into(), json!(window.label())); } }
    let from_pet = window.label()=="pet";
    let backend = Arc::clone(&backend);
    let allowed = Arc::clone(&views.0);
    tauri::async_runtime::spawn_blocking(move || -> Result<Value,String> {
        // Phase 24 (V1-055, t24-ux-shell): the pet may only narrow standing authority (the checks above already limit
        // its `setting` to permissions). A wider mode or a tool turned on is refused, compared with the permissions the
        // host holds just before the change; the workspace's Settings is where authority grows.
        if from_pet && method=="setting" && pet_widens(&backend, &params)? { return Err("Open the workspace to turn this on.".into()); }
        let result = backend.call(&method, params.clone(), Duration::from_secs(180));
        if method=="view_stage" || method=="artifact_view_open" {
            if let Ok(ref value)=result { if let Some(url)=value.get("url").and_then(Value::as_str) {
                if let Ok(parsed)=tauri::Url::parse(url) { if parsed.scheme()=="http" && parsed.host_str()==Some("127.0.0.1") && parsed.path().starts_with("/view/") { allowed.lock().unwrap().insert(url.to_owned()); } }
            } }
        }
        if method=="view_stop" { if let Some(url)=params.get("url").and_then(Value::as_str) { allowed.lock().unwrap().remove(url); } }
        result
    }).await.map_err(|e|e.to_string())?
}

#[tauri::command]
fn compact(window:tauri::WebviewWindow, enabled:bool) -> Result<(),String> {
    window.set_size(tauri::LogicalSize::new(if enabled {416.0} else {1160.0},if enabled {580.0} else {800.0})).map_err(|e|e.to_string())?;
    window.set_always_on_top(enabled).map_err(|e|e.to_string())?;
    Ok(())
}

fn reveal(app:&tauri::AppHandle) {
    // Phase 33: under Stealth Lockdown the workspace stays out of the Dock and ⌘Tab and does not take focus.
    #[cfg(target_os="macos")]
    if !stealth_on() { let _=app.set_activation_policy(tauri::ActivationPolicy::Regular); }
    if let Some(window) = app.get_webview_window("main") { let _=window.show(); let _=window.unminimize(); focus(&window); }
}

/// set_focus activates Pebble; under Stealth Lockdown the window is only brought forward and made key, which a
/// non-activating panel takes without activating the app.
fn focus(window:&tauri::WebviewWindow) {
    #[cfg(target_os="macos")]
    if stealth_on() {
        if let Ok(ns)=window.ns_window() { stealth_mac::front_without_activating(ns as *mut objc2::runtime::AnyObject); }
        return;
    }
    let _=window.set_focus();
}

#[tauri::command]
fn show_pet(app:tauri::AppHandle) { to_pet(&app); }

// Phase 31 (1.0.3, D43): the full workspace. The pebble's "Open full workspace" opens the workspace in macOS full
// screen, with the Dock hidden and the standard title bar (so its red, yellow and green buttons show); "Floating
// companion", the red button and ⌘W leave full screen first and then go back to the floating pebble. Hiding a window
// that is still in its own full-screen Space would leave that Space empty.
#[cfg(target_os="macos")]
fn appkit_fullscreen(ns:tauri::Result<*mut std::ffi::c_void>) -> bool {
    use objc2::{msg_send, runtime::AnyObject};
    const FULL_SCREEN: usize = 1 << 14; // NSWindowStyleMaskFullScreen: set until the exit transition has ended
    match ns {
        Ok(ns) if !ns.is_null() => { let mask: usize = unsafe { msg_send![ns as *mut AnyObject, styleMask] }; mask & FULL_SCREEN != 0 }
        _ => false,
    }
}
#[cfg(target_os="macos")]
fn main_fullscreen(app:&tauri::AppHandle) -> bool { app.get_webview_window("main").map(|w| appkit_fullscreen(w.ns_window())).unwrap_or(false) }
#[cfg(not(target_os="macos"))]
fn main_fullscreen(app:&tauri::AppHandle) -> bool { app.get_webview_window("main").and_then(|w| w.is_fullscreen().ok()).unwrap_or(false) }

fn to_pet(app:&tauri::AppHandle) {
    if !main_fullscreen(app) { return pet_now(app); }
    if let Some(main)=app.get_webview_window("main") { let _=main.set_fullscreen(false); }
    let handle=app.clone();
    let _=std::thread::Builder::new().name("pebble-leave-fullscreen".into()).spawn(move || {
        let deadline=Instant::now()+Duration::from_secs(5);
        loop {
            std::thread::sleep(Duration::from_millis(100));
            let (tx, rx) = mpsc::channel();
            let probe=handle.clone();
            let _=handle.run_on_main_thread(move || { let _=tx.send(main_fullscreen(&probe)); });
            if !rx.recv_timeout(Duration::from_secs(1)).unwrap_or(true) || Instant::now()>deadline { break; }
        }
        std::thread::sleep(Duration::from_millis(400)); // the desktop's slide-in after the transition
        let shown=handle.clone();
        let _=handle.run_on_main_thread(move || pet_now(&shown));
    });
}

fn pet_now(app:&tauri::AppHandle) {
    if let Some(w)=app.get_webview_window("pet") { let _=w.show(); }
    if let Some(w)=app.get_webview_window("main") { let _=w.hide(); }
    #[cfg(target_os="macos")]
    let _=app.set_activation_policy(tauri::ActivationPolicy::Accessory);
}

/// The main window's title bar follows full screen: the standard (opaque) bar in full screen, where macOS keeps an
/// overlay bar's strip hidden and its buttons with it, and the overlay bar otherwise.
fn sync_title_bar(window:&tauri::Window) {
    #[cfg(target_os="macos")]
    if window.label()=="main" {
        let full=appkit_fullscreen(window.ns_window());
        if MAIN_FULLSCREEN.swap(full, Ordering::SeqCst)!=full {
            let _=window.set_title_bar_style(if full {tauri::TitleBarStyle::Visible} else {tauri::TitleBarStyle::Overlay});
        }
    }
}
static MAIN_FULLSCREEN: AtomicBool = AtomicBool::new(false);

/// While the workspace is in full screen the Dock is hidden instead of sliding up over the message box. tao's window
/// delegate (TaoWindowDelegate, tao 0.37) answers window:willUseFullScreenPresentationOptions: with the proposed
/// options outside its exclusive mode; this keeps them and swaps AutoHideDock for HideDock. The menu bar keeps the
/// person's own setting.
#[cfg(target_os="macos")]
fn hide_dock_in_fullscreen() -> bool {
    use objc2::{ffi, runtime::{AnyClass, AnyObject, Imp, Sel}, sel};
    extern "C-unwind" fn options(_this: *mut AnyObject, _sel: Sel, _window: *mut AnyObject, proposed: usize) -> usize {
        const AUTO_HIDE_DOCK: usize = 1 << 0;
        const HIDE_DOCK: usize = 1 << 1;
        (proposed & !AUTO_HIDE_DOCK) | HIDE_DOCK
    }
    let Some(class)=AnyClass::get(c"TaoWindowDelegate") else { return false };
    let imp: Imp = unsafe { std::mem::transmute(options as extern "C-unwind" fn(*mut AnyObject, Sel, *mut AnyObject, usize) -> usize) };
    unsafe { ffi::class_replaceMethod(class as *const AnyClass as *mut AnyClass, sel!(window:willUseFullScreenPresentationOptions:), imp, c"Q@:@Q".as_ptr()) };
    true
}

// Phase 33 (1.1.0; D45-D47, D57): Stealth Lockdown. One switch turns on both halves, for privacy and so Pebble never
// interrupts the app you are in:
//   - Stealth: every Pebble window is left out of screen capture and screen sharing (NSWindowSharingNone), the
//     workspace drops its shadow, Pebble stays out of the Dock and ⌘Tab (Accessory) and its windows leave Mission
//     Control (transient). ⌃⌥⌘H hides Pebble at once; the menu-bar icon brings it back.
//   - Lockdown: Pebble never takes keyboard focus from the app in front. Its windows become non-activating panels, so
//     typing reaches Pebble while the other app stays the active one, and nothing calls set_focus (which activates).
// Hidden from screen capture and screen sharing, not from a camera, a capture card or a process list. Exams and
// interviews that forbid assistants still forbid it.
static STEALTH: AtomicBool = AtomicBool::new(false);

fn stealth_on() -> bool { STEALTH.load(Ordering::SeqCst) }

#[cfg(target_os="macos")]
mod stealth_mac {
    use objc2::{ffi, msg_send, runtime::{AnyClass, AnyObject, Bool}, sel};
    use std::collections::HashMap;
    use std::sync::{Mutex, OnceLock};

    const NONACTIVATING_PANEL: usize = 1 << 7; // NSWindowStyleMaskNonactivatingPanel: honoured only by an NSPanel
    const MANAGED: usize = 1 << 2; // NSWindowCollectionBehaviorManaged: shown in Mission Control
    const TRANSIENT: usize = 1 << 3; // NSWindowCollectionBehaviorTransient: left out of Mission Control
    static BEHAVIOR: Mutex<Option<HashMap<String, usize>>> = Mutex::new(None);

    /// NSPanel carrying TaoWindow's own ivar and methods (tao 0.37.1: `focusable`, canBecomeMainWindow,
    /// canBecomeKeyWindow, sendEvent:), so a swapped window keeps tao's behaviour. None when the runtime layout does
    /// not allow a safe swap (NSPanel instances larger than NSWindow's, or the ivar at another offset); lockdown then
    /// keeps windows as they are and only never calls set_focus.
    fn panel_class() -> Option<&'static AnyClass> {
        static CLASS: OnceLock<Option<usize>> = OnceLock::new();
        let made = *CLASS.get_or_init(|| unsafe {
            let tao = AnyClass::get(c"TaoWindow")?;
            let panel = AnyClass::get(c"NSPanel")?;
            let window = AnyClass::get(c"NSWindow")?;
            if ffi::class_getInstanceSize(panel) != ffi::class_getInstanceSize(window) { return None; }
            let cls = ffi::objc_allocateClassPair(panel, c"PebbleStealthPanel".as_ptr(), 0);
            if cls.is_null() { return None; }
            if !ffi::class_addIvar(cls, c"focusable".as_ptr(), 1, 0, c"c".as_ptr()).as_bool() { return None; }
            for name in [sel!(canBecomeMainWindow), sel!(canBecomeKeyWindow), sel!(sendEvent:)] {
                let method = ffi::class_getInstanceMethod(tao, name);
                let imp = ffi::method_getImplementation(method)?;
                if !ffi::class_addMethod(cls, name, imp, ffi::method_getTypeEncoding(method)).as_bool() { return None; }
            }
            ffi::objc_registerClassPair(cls);
            let offset = |c: *const AnyClass| ffi::ivar_getOffset(ffi::class_getInstanceVariable(c, c"focusable".as_ptr()));
            if offset(cls) != offset(tao) || ffi::class_getInstanceSize(cls) > ffi::class_getInstanceSize(tao) { return None; }
            Some(cls as usize)
        });
        made.map(|ptr| unsafe { &*(ptr as *const AnyClass) })
    }

    /// Lockdown for one window: a non-activating panel while on, tao's own window class again when off. Returns
    /// whether the window is a panel now.
    pub fn set_panel(ns: *mut AnyObject, on: bool) -> bool {
        let (Some(tao), Some(panel)) = (AnyClass::get(c"TaoWindow"), panel_class()) else { return false };
        unsafe {
            let class = ffi::object_getClass(ns);
            let mask: usize = msg_send![ns, styleMask];
            if on && class == tao as *const AnyClass {
                ffi::object_setClass(ns, panel);
                let _: () = msg_send![ns, setStyleMask: mask | NONACTIVATING_PANEL];
                let _: () = msg_send![ns, setBecomesKeyOnlyIfNeeded: Bool::NO];
            } else if !on && class == panel as *const AnyClass {
                let _: () = msg_send![ns, setStyleMask: mask & !NONACTIVATING_PANEL];
                ffi::object_setClass(ns, tao);
            }
            ffi::object_getClass(ns) == panel as *const AnyClass
        }
    }

    /// Mission Control: transient while on; the window's own behaviour restored when off.
    pub fn set_mission_control(label: &str, ns: *mut AnyObject, on: bool) {
        let mut saved = BEHAVIOR.lock().unwrap();
        let saved = saved.get_or_insert_with(HashMap::new);
        unsafe {
            let current: usize = msg_send![ns, collectionBehavior];
            if on {
                saved.entry(label.to_string()).or_insert(current);
                let _: () = msg_send![ns, setCollectionBehavior: (current & !MANAGED) | TRANSIENT];
            } else if let Some(original) = saved.remove(label) {
                let _: () = msg_send![ns, setCollectionBehavior: original];
            }
        }
    }

    /// Every window the app owns, including AppKit's and plugins' own hidden ones (a probe found an unnamed 500 pt one
    /// still shareable), so no Pebble window is left capturable; the named windows also get Tauri's own call.
    pub fn set_sharing_all(on: bool) {
        unsafe {
            let app: *mut AnyObject = msg_send![objc2::class!(NSApplication), sharedApplication];
            let windows: *mut AnyObject = msg_send![app, windows];
            let count: usize = msg_send![windows, count];
            for index in 0..count {
                let window: *mut AnyObject = msg_send![windows, objectAtIndex: index];
                let _: () = msg_send![window, setSharingType: if on { 0usize } else { 1usize }]; // None, ReadOnly
            }
        }
    }

    /// Bring a window forward and make it key without activating Pebble (a panel becomes key while another app stays
    /// active); set_focus would activate the app.
    pub fn front_without_activating(ns: *mut AnyObject) {
        unsafe { let _: () = msg_send![ns, makeKeyAndOrderFront: std::ptr::null::<AnyObject>()]; }
    }
}

/// Apply or lift Stealth Lockdown on every window and the menu-bar icon. Runs on AppKit's main thread.
///
/// Phase 37 (1.5.0, the person's report on 1.4.0): a full-screen workspace that turned stealthy came out of full
/// screen still screen-sized, the Dock showing and its red, yellow and green buttons gone, because it became a panel
/// while macOS was still animating it out of its Space. Capture protection, the Dock and ⌘Tab still change at once;
/// only that window's panel switch waits for the exit to end (windowed_after_full_screen), and it then becomes an
/// ordinary-sized window, centred. Turning stealth off returns it to full screen.
fn apply_stealth(app:&tauri::AppHandle, on:bool) {
    STEALTH.store(on, Ordering::SeqCst);
    for label in ["pet", "main"] {
        let Some(window)=app.get_webview_window(label) else { continue };
        let _=window.set_content_protected(on);
        let mut leaving=false;
        if label=="main" {
            let _=window.set_shadow(!on);
            #[cfg(target_os="macos")]
            { leaving=on && appkit_fullscreen(window.ns_window()); }
            if on && window.is_fullscreen().unwrap_or(false) { let _=window.set_fullscreen(false); }
            if !on && LEFT_FULL_SCREEN.swap(false, Ordering::SeqCst) && window.is_visible().unwrap_or(false) {
                RETURN_TO_FULL_SCREEN.store(true, Ordering::SeqCst);
            }
        }
        #[cfg(target_os="macos")]
        if let Ok(ns)=window.ns_window() {
            let ns=ns as *mut objc2::runtime::AnyObject;
            stealth_mac::set_mission_control(label, ns, on);
            if leaving { LEFT_FULL_SCREEN.store(true, Ordering::SeqCst); windowed_after_full_screen(app); }
            else { let _=stealth_mac::set_panel(ns, on); }
        }
    }
    #[cfg(target_os="macos")]
    {
        stealth_mac::set_sharing_all(on);
        let workspace_open=app.get_webview_window("main").and_then(|w| w.is_visible().ok()).unwrap_or(false);
        let _=app.set_activation_policy(if on || !workspace_open {tauri::ActivationPolicy::Accessory} else {tauri::ActivationPolicy::Regular});
    }
    // Back to full screen once Pebble is an ordinary app again (Dock and ⌘Tab), as the workspace was before stealth.
    if let Some(main)=app.get_webview_window("main") {
        if RETURN_TO_FULL_SCREEN.swap(false, Ordering::SeqCst) { let _=main.set_fullscreen(true); }
    }
    tray_state(app, on);
}

static LEFT_FULL_SCREEN: AtomicBool = AtomicBool::new(false); // stealth took the workspace out of full screen
static RETURN_TO_FULL_SCREEN: AtomicBool = AtomicBool::new(false);

/// Once the workspace has left its full-screen Space (as to_pet waits for it), and only if stealth is still on: an
/// ordinary window, the configured size within 80 % of the screen, centred, then the non-activating panel.
#[cfg(target_os="macos")]
fn windowed_after_full_screen(app:&tauri::AppHandle) {
    let handle=app.clone();
    let _=std::thread::Builder::new().name("pebble-stealth-windowed".into()).spawn(move || {
        let deadline=Instant::now()+Duration::from_secs(5);
        loop {
            std::thread::sleep(Duration::from_millis(100));
            let (tx, rx) = mpsc::channel();
            let probe=handle.clone();
            let _=handle.run_on_main_thread(move || { let _=tx.send(main_fullscreen(&probe)); });
            if !rx.recv_timeout(Duration::from_secs(1)).unwrap_or(true) || Instant::now()>deadline { break; }
        }
        std::thread::sleep(Duration::from_millis(400)); // the desktop's slide-in after the transition
        let shown=handle.clone();
        let _=handle.run_on_main_thread(move || {
            if !stealth_on() { return; }
            let Some(main)=shown.get_webview_window("main") else { return };
            let (mut width, mut height)=(WINDOWED_SIZE.0, WINDOWED_SIZE.1);
            if let Ok(Some(monitor))=main.current_monitor() {
                let screen=monitor.size().to_logical::<f64>(monitor.scale_factor());
                width=width.min(screen.width*0.8);
                height=height.min(screen.height*0.8);
            }
            let _=main.set_size(tauri::LogicalSize::new(width, height));
            let _=main.center();
            if let Ok(ns)=main.ns_window() { let _=stealth_mac::set_panel(ns as *mut objc2::runtime::AnyObject, true); }
        });
    });
}
#[cfg(not(target_os="macos"))]
fn windowed_after_full_screen(_app:&tauri::AppHandle) {}
const WINDOWED_SIZE: (f64, f64) = (1160.0, 800.0); // the workspace's configured size (tauri.conf.json)

/// The settings switch (main window only) applies the state the backend now holds.
#[tauri::command]
fn stealth_set(window:tauri::WebviewWindow, app:tauri::AppHandle, enabled:bool) -> Result<(),String> {
    main_only(&window)?;
    let handle=app.clone();
    app.run_on_main_thread(move || apply_stealth(&handle, enabled)).map_err(|e|e.to_string())
}

/// ⌃⌥⌘S and the menu's Stealth Lockdown item: the backend's setting first (every window re-reads it), then the shell.
fn stealth_toggle(app:&tauri::AppHandle) {
    let next=!stealth_on();
    let backend=Arc::clone(&*app.state::<Arc<Backend>>());
    let handle=app.clone();
    let _=std::thread::Builder::new().name("pebble-stealth".into()).spawn(move || {
        if backend.call("setting", json!({"key":"stealth","value":next}), Duration::from_secs(10)).is_ok() {
            let shown=handle.clone();
            let _=handle.run_on_main_thread(move || apply_stealth(&shown, next));
        }
    });
}

/// ⌃⌥⌘H and "Hide Pebble now": every Pebble window hidden at once; the menu-bar icon or ⌘⇧Space brings it back.
fn stealth_panic(app:&tauri::AppHandle) {
    for label in ["pet", "main"] { if let Some(w)=app.get_webview_window(label) { let _=w.hide(); } }
}

/// The menu-bar icon: a template glyph (an outline pebble; filled with a ring while on) and the item's check mark.
fn tray_state(app:&tauri::AppHandle, on:bool) {
    if let Some(tray)=app.tray_by_id("pebble") {
        let glyph=if on {include_bytes!("../icons/tray-on.png").as_slice()} else {include_bytes!("../icons/tray-off.png").as_slice()};
        if let Ok(image)=tauri::image::Image::from_bytes(glyph) { let _=tray.set_icon_with_as_template(Some(image), true); }
    }
    if let Some(item)=app.state::<StealthItem>().0.lock().unwrap().as_ref() { let _=item.set_checked(on); }
}
struct StealthItem(Mutex<Option<tauri::menu::CheckMenuItem<tauri::Wry>>>);

#[tauri::command]
fn open_workspace(app:tauri::AppHandle, chat_id:Option<String>) -> Result<(),String> {
    reveal(&app);
    if let Some(main)=app.get_webview_window("main") {
        let _=main.set_always_on_top(false); // the floating companion size is always on top; full screen is not
        // Phase 33: under Stealth Lockdown the workspace opens windowed; its own full-screen Space would take over
        // the screen the person is presenting or sharing.
        if !stealth_on() && !main.is_fullscreen().unwrap_or(false) { let _=main.set_fullscreen(true); }
    }
    if let Some(id)=chat_id {
        if id.len()!=32 || !id.chars().all(|c|c.is_ascii_hexdigit()) { return Err("Invalid conversation.".into()); }
        let _=app.emit_to("main","workspace-chat",id);
    }
    Ok(())
}

#[tauri::command]
fn login_item_status(window:tauri::WebviewWindow) -> Result<Value,String> { main_only(&window)?; Ok(json!({"status": login_item::status()})) }
#[tauri::command]
fn login_item_set(window:tauri::WebviewWindow, enabled:bool) -> Result<Value,String> { main_only(&window)?; Ok(json!({"status": login_item::set(enabled)})) }

#[cfg(target_os="macos")]
mod login_item {
    use objc2::{msg_send, rc::Retained, runtime::{AnyClass, AnyObject, Bool}};
    #[link(name = "ServiceManagement", kind = "framework")]
    extern "C" {}
    /// SMAppService.mainAppService (macOS 13+) through runtime messages; no new crate.
    fn service() -> Option<Retained<AnyObject>> {
        let class = AnyClass::get(c"SMAppService")?;
        unsafe { msg_send![class, mainAppService] }
    }
    /// The real status: enabled, requiresApproval, notRegistered or notFound (unsigned dev builds often report notFound).
    pub fn status() -> &'static str {
        let Some(service) = service() else { return "unavailable" };
        let code: isize = unsafe { msg_send![&*service, status] };
        match code { 0 => "not_registered", 1 => "enabled", 2 => "requires_approval", 3 => "not_found", _ => "unavailable" }
    }
    pub fn set(enabled: bool) -> &'static str {
        let Some(service) = service() else { return "unavailable" };
        let error: *mut *mut AnyObject = std::ptr::null_mut(); // NULL out-parameter; the status that follows is the answer
        let _: Bool = unsafe { if enabled { msg_send![&*service, registerAndReturnError: error] } else { msg_send![&*service, unregisterAndReturnError: error] } };
        status()
    }
}
#[cfg(not(target_os="macos"))]
mod login_item {
    pub fn status() -> &'static str { "unavailable" }
    pub fn set(_enabled: bool) -> &'static str { "unavailable" }
}

#[tauri::command]
async fn notification_authorization(window:tauri::WebviewWindow, request:bool) -> Result<Value,String> {
    main_only(&window)?;
    #[cfg(target_os="macos")]
    let status = tauri::async_runtime::spawn_blocking(move || un::center().map_or("unavailable", |center| un::authorization(&center, request))).await.map_err(|e| e.to_string())?;
    #[cfg(not(target_os="macos"))]
    let status = { let _ = request; "unavailable" };
    Ok(json!({"status": status}))
}

#[tauri::command]
fn service_restart(window:tauri::WebviewWindow, app:tauri::AppHandle, backend:tauri::State<'_, Arc<Backend>>) -> Result<(),String> {
    main_only(&window)?;
    if backend.alive() { return Err("The local service is running.".into()); }
    {
        let mut restarts = backend.restarts.lock().unwrap();
        let wait = Duration::from_secs([1, 5, 30][restarts.0.min(2)]);
        if let Some(last) = restarts.1 { if last.elapsed() < wait { return Err(format!("Try again in {} s.", (wait - last.elapsed()).as_secs() + 1)); } }
        *restarts = (restarts.0 + 1, Some(Instant::now()));
    }
    backend.stop(); // reap the dead child and drop its stdin
    // Startup recovery marks unknown outcomes and replays nothing.
    spawn_backend(&app, &backend)?;
    if let Some(wake) = backend.outbox.lock().unwrap().as_ref() { let _ = wake.send(()); }
    Ok(())
}

/// The native Save dialog binds a single-use destination token; the webview never sees or sends a path.
#[tauri::command]
async fn export_choose_destination(window:tauri::WebviewWindow, app:tauri::AppHandle, backend:tauri::State<'_, Arc<Backend>>,
    chat_id:String, export_id:String, suggested_name:String, extension:String) -> Result<Value,String> {
    if window.label()!="main" { return Err("Open the workspace to save exports.".into()); }
    if !hex32(&chat_id) || !hex32(&export_id) { return Err("Invalid export.".into()); }
    let (label,ext) = match extension.as_str() { "pdf"=>("PDF document","pdf"), "docx"=>("Word document","docx"),
        "xlsx"=>("Excel workbook","xlsx"), "csv"=>("CSV table","csv"), "html"=>("Web page","html"), _=>return Err("Unsupported export format.".into()) };
    let mut name:String = suggested_name.chars().filter(|c| !c.is_control() && !"/\\:".contains(*c)).take(90).collect();
    if !name.to_lowercase().ends_with(&format!(".{ext}")) { name = format!("Pebble export.{ext}"); }
    let backend = Arc::clone(&backend);
    tauri::async_runtime::spawn_blocking(move || {
        use tauri_plugin_dialog::DialogExt;
        let Some(chosen) = app.dialog().file().set_title("Save export").set_file_name(&name).add_filter(label,&[ext]).blocking_save_file() else { return Ok(Value::Null) };
        let path = chosen.into_path().map_err(|e| e.to_string())?;
        let replace = path.exists(); // the Save panel already asked the person to confirm replacing it
        let bound = native_call(&backend, "export_destination_bind", json!({"chat_id":chat_id,"export_id":export_id,"path":path.to_string_lossy(),"replace":replace}), Duration::from_secs(30))?;
        Ok(json!({"destination_token":bound.get("destination_token"),"name":bound.get("name")})) // no path back to the webview
    }).await.map_err(|e| e.to_string())?
}

/// Phase 18 telemetry export: the native Save dialog binds a single-use token; the file is never replaced.
#[tauri::command]
async fn telemetry_choose_destination(window:tauri::WebviewWindow, app:tauri::AppHandle, backend:tauri::State<'_, Arc<Backend>>) -> Result<Value,String> {
    main_only(&window)?;
    let backend = Arc::clone(&backend);
    tauri::async_runtime::spawn_blocking(move || {
        use tauri_plugin_dialog::DialogExt;
        let Some(chosen) = app.dialog().file().set_title("Save telemetry").set_file_name("pebble-telemetry.json").add_filter("JSON file",&["json"]).blocking_save_file() else { return Ok(Value::Null) };
        let path = chosen.into_path().map_err(|e| e.to_string())?;
        let bound = native_call(&backend, "telemetry_destination_bind", json!({"path":path.to_string_lossy(),"replace":false}), Duration::from_secs(30))?;
        Ok(json!({"destination_token":bound.get("destination_token"),"name":bound.get("name")})) // no path back to the webview
    }).await.map_err(|e| e.to_string())?
}

fn request_quit(app:&tauri::AppHandle) {
    let (app, backend) = (app.clone(), Arc::clone(app.state::<Arc<Backend>>().inner()));
    std::thread::spawn(move || {
        let message = native_call(&backend, "lifecycle_quit_summary", json!({}), Duration::from_secs(5)).ok()
            .and_then(|summary| summary.get("message").and_then(Value::as_str).map(str::to_owned));
        let quit = match message {
            None => true, // no active schedule, or the service is already offline
            Some(message) => {
                use tauri_plugin_dialog::{DialogExt, MessageDialogButtons, MessageDialogKind};
                app.dialog().message(message).title("Quit Pebble?").kind(MessageDialogKind::Warning)
                    .buttons(MessageDialogButtons::OkCancelCustom("Quit".into(), "Keep Pebble open".into())).blocking_show()
            }
        };
        if quit { QUIT_CONFIRMED.store(true, Ordering::SeqCst); app.exit(0); }
    });
}

/// Phase 22: Pebble's home is ~/.pebble (PEBBLE_HOME when it names an absolute path). The previous home, the app
/// data folder, is passed as --legacy-home: the backend moves it once, verified, and never deletes it.
fn pebble_home(app:&tauri::AppHandle) -> Result<(std::path::PathBuf, std::path::PathBuf),String> {
    let legacy = app.path().app_data_dir().map_err(|e| e.to_string())?;
    let home = match std::env::var_os("PEBBLE_HOME").map(std::path::PathBuf::from) {
        Some(path) if path.is_absolute() => path,
        _ => app.path().home_dir().map_err(|e| e.to_string())?.join(".pebble"),
    };
    Ok((home, legacy))
}

/// Start the backend child with the sanitized environment and attach its pipes; used at setup and by service_restart.
fn spawn_backend(app:&tauri::AppHandle, backend:&Arc<Backend>) -> Result<(),String> {
    let (home, legacy) = pebble_home(app)?;
    std::fs::create_dir_all(home.join("logs")).map_err(|e| e.to_string())?;
    let resources = app.path().resource_dir().map_err(|e| e.to_string())?.join("resources");
    let executable = resources.join("backend").join(if cfg!(windows) {"pebble-backend.exe"} else {"pebble-backend"});
    let service_log = home.join("logs").join("service.log");
    // Phase 24 (V1-010): the backend's stderr log starts again above 10 MB; the previous one is kept as service.log.1.
    if std::fs::metadata(&service_log).map(|m| m.len() > 10 * 1024 * 1024).unwrap_or(false) {
        let _ = std::fs::rename(&service_log, home.join("logs").join("service.log.1"));
    }
    let log = std::fs::OpenOptions::new().create(true).append(true).open(&service_log).map_err(|e| e.to_string())?;
    let mut command = Command::new(executable);
    // A process's initial environment is readable by other same-user
    // processes, including Pebble's own confined jobs (KERN_PROCARGS2).
    // Start the backend from an explicit allowlist so shell secrets such
    // as provider API keys never enter its exec-time environment.
    #[cfg(unix)] {
        command.env_clear();
        for key in ["PATH","HOME","USER","LOGNAME","SHELL","LANG","LC_ALL","LC_CTYPE","TMPDIR","__CF_USER_TEXT_ENCODING"] {
            if let Ok(value) = std::env::var(key) { command.env(key, value); }
        }
    }
    #[cfg(windows)] {
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x08000000);
    }
    let mut child = command.args(["--home",home.to_str().ok_or("Invalid data folder.")?,"--legacy-home",legacy.to_str().ok_or("Invalid data folder.")?,"--resources",resources.to_str().ok_or("Invalid resources folder.")?])
        .stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(log).spawn().map_err(|e| e.to_string())?;
    let reader = BufReader::new(child.stdout.take().ok_or("The local service has no output.")?);
    *backend.input.lock().unwrap() = child.stdin.take();
    *backend.child.lock().unwrap() = Some(child);
    let (pending, handle, outbox, owner) = (backend.pending.clone(), app.clone(), backend.outbox.lock().unwrap().clone(), Arc::clone(backend));
    std::thread::spawn(move || {
        for line in reader.lines().map_while(Result::ok) {
            if let Ok(value) = serde_json::from_str::<Value>(&line) {
                if let Some(id)=value.get("id").and_then(Value::as_str) {
                    if let Some(sender)=pending.lock().unwrap().remove(id) {
                        let result=if let Some(error)=value.get("error").and_then(Value::as_str) {Err(match value.get("code").and_then(Value::as_str) {
                            // Phase 24: main.py's content-free error code; bridge.ts splits it off, native_call drops it.
                            Some(code) if !code.is_empty() && code.len()<=64 && code.bytes().all(|b| b.is_ascii_lowercase()||b.is_ascii_digit()||b==b'_') => format!("{code}\u{1f}{error}"),
                            _ => error.to_owned(),
                        })} else {Ok(value.get("result").cloned().unwrap_or(Value::Null))};
                        let _=sender.send(result);
                    }
                } else if value.get("event").and_then(Value::as_str)==Some("native_export_pdf") {
                    // Carries a single-use attach token and app-data paths: never emitted to webviews. This build
                    // announces no native PDF support, so a stray job fails closed and the export stays unavailable.
                    let job = value.get("data").cloned().unwrap_or(Value::Null);
                    let owner = Arc::clone(&owner);
                    std::thread::spawn(move || {
                        let field = |key:&str| job.get(key).and_then(Value::as_str).unwrap_or("").to_owned();
                        let _ = native_call(&owner, "export_pdf_failed", json!({"chat_id":field("chat_id"),"export_id":field("export_id"),
                            "attach_token":field("attach_token"),"reason":"This build cannot print PDFs."}), Duration::from_secs(30));
                    });
                } else if value.get("event").is_some() {
                    if value.get("event").and_then(Value::as_str)==Some("notification_outbox") { if let Some(ref wake)=outbox { let _=wake.send(()); } }
                    let _=handle.emit("backend-event",&value);
                }
            }
        }
        for (_,sender) in pending.lock().unwrap().drain() { let _=sender.send(Err("Local service stopped.".into())); }
        let _=handle.emit("backend-event",json!({"event":"offline","data":{}}));
    });
    Ok(())
}

/// UNUserNotificationCenter through runtime messages (no new crate): post, delivered-check, remove, and an Open-only delegate.
#[cfg(target_os="macos")]
mod un {
    use std::{collections::HashMap, ptr::NonNull, sync::mpsc, time::Duration};
    use block2::RcBlock;
    use objc2::{define_class, msg_send, rc::Retained, runtime::{AnyClass, AnyObject, Bool, NSObject, NSObjectProtocol}, AllocAnyThread, DefinedClass};
    use objc2_foundation::{NSArray, NSBundle, NSDictionary, NSSet, NSString};
    use serde_json::Value;
    #[link(name = "UserNotifications", kind = "framework")]
    extern "C" {}

    const CATEGORY: &str = "pebble.open"; // one action only: Open. Never Approve, Reject or Reply.
    const OPTION_SOUND: usize = 1 << 1; // UNAuthorizationOptionSound
    const OPTION_ALERT: usize = 1 << 2; // UNAuthorizationOptionAlert
    const PRESENT_LIST: usize = 1 << 3; // UNNotificationPresentationOptionList
    const PRESENT_BANNER: usize = 1 << 4; // UNNotificationPresentationOptionBanner
    const DEFAULT_ACTION: &str = "com.apple.UNNotificationDefaultActionIdentifier";

    /// UNUserNotificationCenter aborts an unbundled process, so dev builds report "unavailable".
    pub fn center() -> Option<Retained<AnyObject>> {
        let bundle = NSBundle::mainBundle();
        let bundled = bundle.bundleIdentifier().is_some() && bundle.bundlePath().to_string().ends_with(".app");
        if !bundled { return None; }
        let class = AnyClass::get(c"UNUserNotificationCenter")?;
        unsafe { msg_send![class, currentNotificationCenter] }
    }

    pub fn authorization(center: &AnyObject, request: bool) -> &'static str {
        if request {
            let (done, wait) = mpsc::channel::<()>();
            let block = RcBlock::new(move |_granted: Bool, _error: *mut AnyObject| { let _ = done.send(()); });
            unsafe { let _: () = msg_send![center, requestAuthorizationWithOptions: OPTION_ALERT | OPTION_SOUND, completionHandler: &*block]; }
            let _ = wait.recv_timeout(Duration::from_secs(300)); // the person answers the system prompt
        }
        let (done, wait) = mpsc::channel::<isize>();
        let block = RcBlock::new(move |settings: NonNull<AnyObject>| {
            let status: isize = unsafe { msg_send![settings.as_ref(), authorizationStatus] };
            let _ = done.send(status);
        });
        unsafe { let _: () = msg_send![center, getNotificationSettingsWithCompletionHandler: &*block]; }
        // UNAuthorizationStatus: notDetermined 0, denied 1, authorized 2, provisional 3, ephemeral 4.
        match wait.recv_timeout(Duration::from_secs(10)) {
            Ok(0) => "not_determined", Ok(1) => "denied", Ok(2) | Ok(4) => "authorized", Ok(3) => "provisional", _ => "unavailable",
        }
    }

    fn text(object: Option<Retained<AnyObject>>) -> Option<String> {
        object.and_then(|value| value.downcast_ref::<NSString>().map(|text| text.to_string()))
    }
    fn info(content: &AnyObject, key: &str) -> Option<String> {
        let user_info: Option<Retained<AnyObject>> = unsafe { msg_send![content, userInfo] };
        let user_info = user_info?;
        text(unsafe { msg_send![&*user_info, objectForKey: &*NSString::from_str(key)] })
    }

    /// Identifier -> userInfo.version for every banner in Notification Center; None when the check did not answer.
    pub fn delivered(center: &AnyObject) -> Option<HashMap<String, u64>> {
        let (done, wait) = mpsc::channel();
        let block = RcBlock::new(move |list: NonNull<AnyObject>| {
            let mut found = HashMap::new();
            let list = unsafe { list.as_ref() };
            let count: usize = unsafe { msg_send![list, count] };
            for index in 0..count {
                let item: Option<Retained<AnyObject>> = unsafe { msg_send![list, objectAtIndex: index] };
                let Some(item) = item else { continue };
                let request: Option<Retained<AnyObject>> = unsafe { msg_send![&*item, request] };
                let Some(request) = request else { continue };
                let content: Option<Retained<AnyObject>> = unsafe { msg_send![&*request, content] };
                let version = content.and_then(|c| info(&c, "version")).and_then(|v| v.parse().ok()).unwrap_or(0);
                if let Some(id) = text(unsafe { msg_send![&*request, identifier] }) { found.insert(id, version); }
            }
            let _ = done.send(found);
        });
        unsafe { let _: () = msg_send![center, getDeliveredNotificationsWithCompletionHandler: &*block]; }
        wait.recv_timeout(Duration::from_secs(10)).ok()
    }

    /// Ok(true) posted, Ok(false) unknown (retry after the next delivered-check), Err(reason) failed.
    pub fn post(center: &AnyObject, post: &Value, id: &str, version: u64) -> Result<bool, &'static str> {
        let field = |key: &str, fallback: &str| NSString::from_str(post.get(key).and_then(Value::as_str).unwrap_or(fallback));
        let chat = post.get("chat_id").and_then(Value::as_str).filter(|value| super::hex32(value)).unwrap_or("");
        let (Some(content_class), Some(request_class)) = (AnyClass::get(c"UNMutableNotificationContent"), AnyClass::get(c"UNNotificationRequest")) else { return Err("unavailable") };
        let content: Option<Retained<AnyObject>> = unsafe { msg_send![content_class, new] };
        let Some(content) = content else { return Err("error") };
        let (key_version, key_chat) = (NSString::from_str("version"), NSString::from_str("chat_id"));
        let (value_version, value_chat) = (NSString::from_str(&version.to_string()), NSString::from_str(chat));
        let user_info = NSDictionary::<NSString, NSString>::from_slices(&[&*key_version, &*key_chat], &[&*value_version, &*value_chat]);
        unsafe {
            let _: () = msg_send![&*content, setTitle: &*field("title", "Pebble")];
            let _: () = msg_send![&*content, setBody: &*field("body", "")];
            let _: () = msg_send![&*content, setThreadIdentifier: &*field("thread", "pebble")];
            let _: () = msg_send![&*content, setCategoryIdentifier: &*NSString::from_str(CATEGORY)];
            let _: () = msg_send![&*content, setUserInfo: &*user_info];
        }
        let request: Option<Retained<AnyObject>> = unsafe { msg_send![request_class, requestWithIdentifier: &*NSString::from_str(id), content: &*content, trigger: Option::<&AnyObject>::None] };
        let Some(request) = request else { return Err("error") };
        let (done, wait) = mpsc::channel();
        let block = RcBlock::new(move |error: *mut AnyObject| { let _ = done.send(error.is_null()); });
        unsafe { let _: () = msg_send![center, addNotificationRequest: &*request, withCompletionHandler: &*block]; }
        match wait.recv_timeout(Duration::from_secs(10)) { Ok(true) => Ok(true), Ok(false) => Err("error"), Err(_) => Ok(false) }
    }

    pub fn remove(center: &AnyObject, ids: &[String]) {
        let list = NSArray::from_retained_slice(&ids.iter().map(|id| NSString::from_str(id)).collect::<Vec<_>>());
        unsafe {
            let _: () = msg_send![center, removePendingNotificationRequestsWithIdentifiers: &*list];
            let _: () = msg_send![center, removeDeliveredNotificationsWithIdentifiers: &*list];
        }
    }

    define_class!(
        // SAFETY: NSObject has no subclassing requirements, and OpenDelegate does not implement Drop.
        #[unsafe(super(NSObject))]
        #[name = "PebbleNotificationOpen"]
        #[ivars = tauri::AppHandle]
        struct OpenDelegate;

        unsafe impl NSObjectProtocol for OpenDelegate {}

        impl OpenDelegate {
            #[unsafe(method(userNotificationCenter:didReceiveNotificationResponse:withCompletionHandler:))]
            fn did_receive(&self, _center: &AnyObject, response: &AnyObject, handler: &block2::DynBlock<dyn Fn()>) {
                let action = text(unsafe { msg_send![response, actionIdentifier] }).unwrap_or_default();
                // A tap and the single Open action both open the conversation; dismissal does nothing. There is no Approve.
                if action == "open" || action == DEFAULT_ACTION {
                    let notification: Option<Retained<AnyObject>> = unsafe { msg_send![response, notification] };
                    let request: Option<Retained<AnyObject>> = notification.and_then(|n| unsafe { msg_send![&*n, request] });
                    let content: Option<Retained<AnyObject>> = request.and_then(|r| unsafe { msg_send![&*r, content] });
                    let chat = content.and_then(|c| info(&c, "chat_id")).filter(|value| super::hex32(value));
                    let _ = super::open_workspace(self.ivars().clone(), chat); // hex-validated again there
                }
                handler.call(());
            }

            #[unsafe(method(userNotificationCenter:willPresentNotification:withCompletionHandler:))]
            fn will_present(&self, _center: &AnyObject, _notification: &AnyObject, handler: &block2::DynBlock<dyn Fn(usize)>) {
                handler.call((PRESENT_BANNER | PRESENT_LIST,));
            }
        }
    );

    /// Registers the Open-only category and the delegate; call once on the main thread at setup.
    pub fn register(app: tauri::AppHandle) {
        let Some(center) = center() else { return };
        let (Some(action_class), Some(category_class)) = (AnyClass::get(c"UNNotificationAction"), AnyClass::get(c"UNNotificationCategory")) else { return };
        unsafe {
            // UNNotificationActionOptionForeground = 1 << 2; UNNotificationCategoryOptions none = 0.
            let open: Option<Retained<AnyObject>> = msg_send![action_class, actionWithIdentifier: &*NSString::from_str("open"), title: &*NSString::from_str("Open"), options: 1usize << 2];
            let Some(open) = open else { return };
            let actions = NSArray::from_retained_slice(&[open]);
            let empty = NSArray::<AnyObject>::new();
            let category: Option<Retained<AnyObject>> = msg_send![category_class, categoryWithIdentifier: &*NSString::from_str(CATEGORY), actions: &*actions, intentIdentifiers: &*empty, options: 0usize];
            let Some(category) = category else { return };
            let categories = NSSet::from_retained_slice(&[category]);
            let _: () = msg_send![&*center, setNotificationCategories: &*categories];
            let delegate: Retained<OpenDelegate> = msg_send![super(OpenDelegate::alloc().set_ivars(app)), init];
            let _: () = msg_send![&*center, setDelegate: &*delegate];
            std::mem::forget(delegate); // the center holds its delegate weakly; it lives as long as the app
        }
    }
}

/// Pull, check delivered, post or remove, acknowledge. One pull at a time; bursts of outbox events coalesce.
fn start_notification_pump(backend:Arc<Backend>) {
    let (wake, events) = mpsc::channel::<()>();
    *backend.outbox.lock().unwrap() = Some(wake);
    #[cfg(target_os="macos")]
    std::thread::spawn(move || {
        let Some(center) = un::center() else { return }; // unbundled dev build: notices stay in the app
        let mut posted = std::collections::HashSet::<(String, u64)>::new(); // survives a backend restart, not a shell crash
        loop {
            if let Err(mpsc::RecvTimeoutError::Disconnected) = events.recv_timeout(Duration::from_secs(60)) { return; }
            while events.try_recv().is_ok() {}
            let Ok(batch) = native_call(&backend, "notification_native_pending", json!({}), Duration::from_secs(30)) else { continue };
            let (mut delivered, mut removed, mut failed) = (Vec::new(), Vec::new(), Vec::new());
            let remove: Vec<String> = batch["remove"].as_array().into_iter().flatten().filter_map(Value::as_str).filter(|id| hex32(id)).map(str::to_owned).collect();
            if !remove.is_empty() { un::remove(&center, &remove); removed = remove; }
            let posts: Vec<&Value> = batch["post"].as_array().into_iter().flatten().collect();
            if !posts.is_empty() {
                let allowed = matches!(un::authorization(&center, false), "authorized" | "provisional");
                // No answer from the delivered-check means no post this round: an unknown banner is never shown twice.
                let shown = if allowed { un::delivered(&center) } else { None };
                for post in posts {
                    let (Some(id), Some(version)) = (post["id"].as_str().filter(|id| hex32(id)), post["version"].as_u64()) else { continue };
                    if !allowed { failed.push(json!({"id": id, "reason": "not_authorized"})); continue; }
                    let Some(ref shown) = shown else { break };
                    if shown.get(id).is_some_and(|have| *have >= version) || posted.contains(&(id.to_owned(), version)) {
                        delivered.push(json!({"id": id, "version": version}));
                        continue;
                    }
                    match un::post(&center, post, id, version) {
                        Ok(true) => { posted.insert((id.to_owned(), version)); delivered.push(json!({"id": id, "version": version})); }
                        Ok(false) => {} // unknown: the next pull rechecks delivered notifications first
                        Err(reason) => failed.push(json!({"id": id, "reason": reason})),
                    }
                }
            }
            if !(delivered.is_empty() && removed.is_empty() && failed.is_empty()) {
                let _ = native_call(&backend, "notification_native_delivered", json!({"delivered": delivered, "removed": removed, "failed": failed}), Duration::from_secs(30));
            }
        }
    });
    #[cfg(not(target_os="macos"))]
    { let _ = events; }
}

/// NSWorkspace sleep/wake and the system time-zone change, as hints only; the backend detects from its own clocks.
#[cfg(target_os="macos")]
fn observe_lifecycle(backend:Arc<Backend>) {
    use block2::RcBlock;
    use objc2::{msg_send, rc::Retained, runtime::{AnyClass, AnyObject}};
    use objc2_foundation::NSString;
    let send = move |params: Value| { let backend = Arc::clone(&backend); std::thread::spawn(move || { let _ = native_call(&backend, "lifecycle_event", params, Duration::from_secs(10)); }); };
    let (Some(workspace_class), Some(center_class), Some(zone_class)) = (AnyClass::get(c"NSWorkspace"), AnyClass::get(c"NSNotificationCenter"), AnyClass::get(c"NSTimeZone")) else { return };
    unsafe {
        let workspace: Option<Retained<AnyObject>> = msg_send![workspace_class, sharedWorkspace];
        let workspace_center: Option<Retained<AnyObject>> = workspace.and_then(|w| msg_send![&*w, notificationCenter]);
        if let Some(center) = workspace_center {
            for (name, kind) in [("NSWorkspaceWillSleepNotification", "will_sleep"), ("NSWorkspaceDidWakeNotification", "did_wake")] {
                let send = send.clone();
                let block = RcBlock::new(move |_note: std::ptr::NonNull<AnyObject>| send(json!({"kind": kind})));
                let token: Option<Retained<AnyObject>> = msg_send![&*center, addObserverForName: &*NSString::from_str(name), object: Option::<&AnyObject>::None, queue: Option::<&AnyObject>::None, usingBlock: &*block];
                std::mem::forget(token);
            }
        }
        let block = RcBlock::new(move |_note: std::ptr::NonNull<AnyObject>| {
            let _: () = msg_send![zone_class, resetSystemTimeZone];
            let zone: Option<Retained<AnyObject>> = msg_send![zone_class, systemTimeZone];
            let name: Option<Retained<AnyObject>> = zone.and_then(|z| msg_send![&*z, name]);
            if let Some(name) = name.and_then(|n| n.downcast_ref::<NSString>().map(|t| t.to_string())) {
                send(json!({"kind": "timezone_changed", "timezone": name}));
            }
        });
        let default_center: Option<Retained<AnyObject>> = msg_send![center_class, defaultCenter];
        if let Some(center) = default_center {
            let token: Option<Retained<AnyObject>> = msg_send![&*center, addObserverForName: &*NSString::from_str("NSSystemTimeZoneDidChangeNotification"), object: Option::<&AnyObject>::None, queue: Option::<&AnyObject>::None, usingBlock: &*block];
            std::mem::forget(token);
        }
    }
}

/// `status`: the collapsed pet shows a status pill above the avatar while the host reports live work (Phase 19 P8).
#[tauri::command]
fn pet_expand(app:tauri::AppHandle, expanded:bool, sidebar:Option<bool>, status:Option<bool>) -> Result<(),String> {
    let window=app.get_webview_window("pet").ok_or("Companion is unavailable.")?;
    let scale=window.scale_factor().map_err(|e|e.to_string())?;
    let pos=window.outer_position().map_err(|e|e.to_string())?;
    let old=window.inner_size().map_err(|e|e.to_string())?;
    let (mut width,mut height):(f64,f64)=if expanded && sidebar.unwrap_or(false) {(744.0,576.0)} else if expanded {(384.0,456.0)} else if status.unwrap_or(false) {(176.0,136.0)} else {(112.0,112.0)};
    let monitor=window.current_monitor().map_err(|e|e.to_string())?;
    if let Some(ref m)=monitor {
        width=width.min(m.size().width as f64/scale);
        height=height.min((m.size().height as f64/scale-48.0).max(112.0));
    }
    let mut x=pos.x as f64+(old.width as f64-width*scale);
    let mut y=pos.y as f64+(old.height as f64-height*scale);
    if let Some(m)=monitor {
        x=x.max(m.position().x as f64).min(m.position().x as f64+m.size().width as f64-width*scale);
        y=y.max(m.position().y as f64+24.0*scale).min(m.position().y as f64+m.size().height as f64-height*scale);
    }
    window.set_size(tauri::LogicalSize::new(width,height)).map_err(|e|e.to_string())?;
    window.set_position(tauri::PhysicalPosition::new(x as i32,y as i32)).map_err(|e|e.to_string())?;
    if expanded { focus(&window); }
    Ok(())
}

// Phase 19 F4 (design study §2.4): the pet's eyes follow the pointer. A shell thread samples the global cursor
// only while the pet asked for it (companion.pointer on, reduced motion off) and is visible: 15 Hz while the
// cursor is within 400 pt of the avatar and moved in the last 5 s, 2 Hz otherwise. Only a direction quantized
// to 1/8 in [-1, 1] reaches the pet, and only when it changes (null once on leaving the radius); cursor and
// display coordinates never leave the shell. Reading the cursor location monitors no input events.
static GAZE_WANTED: AtomicBool = AtomicBool::new(false);
static GAZE_RUNNING: AtomicBool = AtomicBool::new(false);

#[tauri::command]
fn pet_gaze(window:tauri::WebviewWindow, enabled:bool) -> Result<(),String> {
    if window.label()!="pet" { return Err("Only the companion follows the pointer.".into()); }
    GAZE_WANTED.store(enabled, Ordering::SeqCst);
    if !enabled || GAZE_RUNNING.swap(true, Ordering::SeqCst) { return Ok(()); }
    let app = window.app_handle().clone();
    std::thread::Builder::new().name("pebble-pet-gaze".into()).spawn(move || {
        let step = |v:f64| (v.clamp(-1.0,1.0)*8.0).round()/8.0;
        let (mut last, mut sent, mut moved) = (None::<(f64,f64)>, None::<(f64,f64)>, None::<Instant>);
        loop {
            let mut hidden = false;
            while GAZE_WANTED.load(Ordering::SeqCst) {
                let Some(pet) = app.get_webview_window("pet") else { hidden = true; break };
                if !pet.is_visible().unwrap_or(false) { hidden = true; break; }
                let mut gaze = None;
                if let (Ok(cursor), Ok(origin), Ok(size), Ok(scale)) = (pet.cursor_position(), pet.outer_position(), pet.outer_size(), pet.scale_factor()) {
                    // The avatar is the 112 pt square in the window's bottom-right corner (Pet.vue .pet-avatar).
                    let (cx, cy) = (origin.x as f64 + size.width as f64 - 56.0*scale, origin.y as f64 + size.height as f64 - 56.0*scale);
                    let (dx, dy) = ((cursor.x - cx)/scale, (cursor.y - cy)/scale);
                    if dx.hypot(dy) <= 400.0 {
                        gaze = Some((step(dx/400.0), step(dy/400.0)));
                        if last != Some((cursor.x, cursor.y)) { moved = Some(Instant::now()); }
                    }
                    last = Some((cursor.x, cursor.y));
                }
                if gaze != sent {
                    sent = gaze;
                    let _ = app.emit_to("pet", "pet-gaze", gaze.map(|(x,y)| json!({"x":x,"y":y})).unwrap_or(Value::Null));
                }
                let lively = gaze.is_some() && moved.is_some_and(|t| t.elapsed() < Duration::from_secs(5));
                std::thread::sleep(Duration::from_millis(if lively {66} else {500}));
            }
            GAZE_RUNNING.store(false, Ordering::SeqCst);
            // An enable that arrived while this loop was ending found it still running: take that request over.
            if hidden || !GAZE_WANTED.load(Ordering::SeqCst) || GAZE_RUNNING.swap(true, Ordering::SeqCst) { break; }
        }
        if sent.is_some() { let _ = app.emit_to("pet", "pet-gaze", Value::Null); } // the eyes rest
    }).map_err(|e| { GAZE_RUNNING.store(false, Ordering::SeqCst); e.to_string() })?;
    Ok(())
}

fn configure_pet(app:&tauri::AppHandle) -> tauri::Result<()> {
    let window=app.get_webview_window("pet").expect("Pet window exists");
    if let Some(m)=window.primary_monitor()? {
        let scale=m.scale_factor();
        window.set_position(tauri::PhysicalPosition::new(m.position().x+m.size().width as i32-(150.0*scale) as i32,m.position().y+m.size().height as i32-(175.0*scale) as i32))?;
    }
    #[cfg(target_os="macos")]
    {
        use objc2_app_kit::{NSWindow, NSWindowCollectionBehavior as B};
        // Setup runs on AppKit's main thread. Tauri owns this NSWindow for its lifetime.
        let native:&NSWindow=unsafe { &*window.ns_window()?.cast() };
        let mut behavior=B::CanJoinAllSpaces | B::FullScreenAuxiliary | B::IgnoresCycle;
        if objc2_foundation::NSProcessInfo::processInfo().operatingSystemVersion().majorVersion>=13 { behavior |= B::CanJoinAllApplications; }
        native.setCollectionBehavior(behavior);
        native.setLevel(3); // NSFloatingWindowLevel; no focus-stealing elevated system level.
        native.setHidesOnDeactivate(false);
        app.set_activation_policy(tauri::ActivationPolicy::Accessory)?;
    }
    Ok(())
}

fn main() {
    use tauri_plugin_global_shortcut::{Code, GlobalShortcutExt, Modifiers, ShortcutState};
    let application = tauri::Builder::default()
        .manage(AllowedViews::default())
        .plugin(tauri::plugin::Builder::<tauri::Wry, ()>::new("navigation-guard").on_navigation(|webview,url| {
            host_url(url) || url.as_str()=="about:blank" || webview.app_handle().state::<AllowedViews>().0.lock().unwrap().contains(url.as_str())
        }).build())
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_global_shortcut::Builder::new().with_handler(|app,shortcut,event| {
            if event.state!=ShortcutState::Pressed { return; }
            // Phase 33: ⌃⌥⌘S toggles Stealth Lockdown, ⌃⌥⌘H hides every Pebble window; ⌘⇧Space opens the workspace.
            let stealth_keys=Modifiers::CONTROL | Modifiers::ALT | Modifiers::SUPER;
            if shortcut.matches(stealth_keys, Code::KeyS) { stealth_toggle(app); }
            else if shortcut.matches(stealth_keys, Code::KeyH) { stealth_panic(app); }
            else { reveal(app); }
        }).build())
        // The app manifest puts custom commands under Tauri's origin-aware ACL,
        // alongside plugin commands. No remote capability grants canvas frames
        // access. Do not register their loopback origin as a local protocol.
        .invoke_handler(tauri::generate_handler![rpc,compact,open_workspace,pet_expand,pet_gaze,show_pet,stealth_set,login_item_status,login_item_set,notification_authorization,service_restart,export_choose_destination,telemetry_choose_destination])
        .setup(|app| {
            let backend = Arc::new(Backend { input:Mutex::new(None), child:Mutex::new(None), pending:Arc::new(Mutex::new(HashMap::new())),
                sequence:AtomicU64::new(1), restarts:Mutex::new((0, None)), outbox:Mutex::new(None) });
            app.manage(Arc::clone(&backend));
            start_notification_pump(Arc::clone(&backend)); // installs the wake channel the reader uses
            spawn_backend(app.handle(), &backend)?;
            if let Some(wake) = backend.outbox.lock().unwrap().as_ref() { let _ = wake.send(()); } // a first pull
            #[cfg(target_os="macos")]
            {
                un::register(app.handle().clone());
                observe_lifecycle(Arc::clone(&backend));
            }
            let _ = app.global_shortcut().register("CommandOrControl+Shift+Space");
            let _ = app.global_shortcut().register("Control+Alt+Command+S");
            let _ = app.global_shortcut().register("Control+Alt+Command+H");
            use tauri::menu::{CheckMenuItem,Menu,MenuItem,PredefinedMenuItem};
            // Phase 33: the menu-bar icon is a template glyph (it changes shape while Stealth Lockdown is on) and its
            // menu carries the switch and Hide Pebble now; with Pebble out of the Dock and ⌘Tab it is the way back in.
            let stealth=CheckMenuItem::with_id(app,"stealth","Stealth Lockdown",true,false,Some("Control+Alt+Command+S"))?;
            let hide=MenuItem::with_id(app,"hide","Hide Pebble now",true,Some("Control+Alt+Command+H"))?;
            let show=MenuItem::with_id(app,"show","Open Pebble",true,None::<&str>)?;
            let quit=MenuItem::with_id(app,"quit","Quit Pebble",true,None::<&str>)?;
            let menu=Menu::with_items(app,&[&stealth,&hide,&PredefinedMenuItem::separator(app)?,&show,&quit])?;
            app.manage(StealthItem(Mutex::new(Some(stealth.clone()))));
            let glyph=tauri::image::Image::from_bytes(include_bytes!("../icons/tray-off.png"))?;
            let _tray=tauri::tray::TrayIconBuilder::with_id("pebble").icon(glyph).icon_as_template(true).tooltip("Pebble").menu(&menu).on_menu_event(|app,event| match event.id.as_ref() {
                "stealth"=>stealth_toggle(app),"hide"=>stealth_panic(app),"show"=>reveal(app),"quit"=>request_quit(app),_=>{}
            }).build(app)?;
            configure_pet(app.handle())?;
            #[cfg(target_os="macos")]
            let _ = hide_dock_in_fullscreen(); // the windows exist, so tao's delegate class is registered
            // Phase 24 (V1-038, t24-ux-shell): with no model connected (a first launch), show the workspace, whose
            // welcome offers "Connect your ChatGPT account" and "Use a local model"; otherwise Pebble starts as the
            // pet, as before. Read off the main thread: the backend may still be starting. A backend that does not
            // answer (or an upgrade waiting in maintenance, which reports no model) also shows the workspace.
            {
                let (handle, backend) = (app.handle().clone(), Arc::clone(&backend));
                let _ = std::thread::Builder::new().name("pebble-first-run".into()).spawn(move || {
                    let state = backend.call("state", json!({}), Duration::from_secs(60)).ok();
                    // Phase 33: Stealth Lockdown survives a restart.
                    if state.as_ref().and_then(|s| s.pointer("/settings/stealth").and_then(Value::as_bool)) == Some(true) {
                        let shown = handle.clone();
                        let _ = handle.run_on_main_thread(move || apply_stealth(&shown, true));
                    }
                    let configured = state.as_ref().and_then(|state| state.pointer("/model/configured").and_then(Value::as_bool));
                    if configured != Some(true) {
                        let shown = handle.clone();
                        let _ = handle.run_on_main_thread(move || reveal(&shown));
                    }
                });
            }
            Ok(())
        })
        .on_window_event(|window,event| match event {
            // Phase 31 (D43): closing the full workspace leaves full screen first, then goes back to the pebble.
            tauri::WindowEvent::CloseRequested{api,..} if window.label()=="main" => { api.prevent_close(); to_pet(window.app_handle()); }
            tauri::WindowEvent::CloseRequested{api,..} => { api.prevent_close(); let _=window.hide(); }
            tauri::WindowEvent::Resized(_) | tauri::WindowEvent::Moved(_) | tauri::WindowEvent::Focused(_) => sync_title_bar(window),
            _ => {}
        })
        .build(tauri::generate_context!()).expect("Could not start Pebble");
    application.run(|app,event| match event {
        // ⌘Q and other user requests confirm first while schedules are active; app.exit(0) carries a code and passes.
        tauri::RunEvent::ExitRequested { api, code: None, .. } if !QUIT_CONFIRMED.load(Ordering::SeqCst) => { api.prevent_exit(); request_quit(app); }
        tauri::RunEvent::Exit => app.state::<Arc<Backend>>().stop(),
        // Phase 24 (V1-128, t24-ux-shell): opening Pebble again (Finder, Spotlight, the Dock) shows the workspace.
        #[cfg(target_os="macos")]
        tauri::RunEvent::Reopen { .. } => reveal(app),
        _ => {}
    });
}

#[cfg(test)]
mod tests {
    use super::widens;
    use serde_json::json;

    // Phase 24 (V1-055): the pet's permission change is compared with the permissions the host holds.
    #[test]
    fn the_pet_may_only_narrow() {
        let held = json!({"mode":"auto","tools":{"files":true,"web":true,"screen":false}});
        assert!(!widens(&held, &json!({"mode":"ask","tools":{"files":true,"web":false,"screen":false}})), "narrowing passes");
        assert!(!widens(&held, &json!({"mode":"auto","tools":{"files":true,"web":true}})), "the same, an omitted optional tool off");
        assert!(widens(&held, &json!({"mode":"full","tools":{"files":true,"web":true,"screen":false}})), "a higher mode");
        assert!(widens(&held, &json!({"mode":"auto","tools":{"files":true,"web":true,"screen":true}})), "a tool that is off now");
        assert!(widens(&held, &json!({"mode":"ask","tools":{"files":true,"web":true,"camera":true}})), "a tool the host does not hold");
        assert!(widens(&held, &json!({"mode":"ask","tools":{"files":"yes"}})), "an unreadable tool value");
        assert!(widens(&held, &json!({"mode":"root","tools":{}})) && widens(&held, &json!({"tools":{}})), "an unknown or missing mode");
        assert!(widens(&json!(null), &json!({"mode":"ask","tools":{}})), "unreadable held permissions fail closed");
        assert!(widens(&held, &json!({"mode":"ask"})), "missing tools fail closed");
    }
}
