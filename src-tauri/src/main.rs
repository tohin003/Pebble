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
    const ALLOWED: &[&str] = &["state","new_chat","get_chat","set_workspace","send","cancel","model_load","model_unload","set_model","approve","reject","apps","attach","attach_url","attach_bytes","remove_source","delete_chat","chat_rename","chat_search","setting","open_app","undo","model_profiles","model_discover","model_save","model_save_many","model_select","model_effort","model_remove","memory_list","memory_save","memory_delete","memory_clear","memory_candidates","memory_maintain","memory_resolve","memory_export","extensions","skill_inspect","hook_events","extension_add","extension_import","extension_toggle","extension_remove","media_artifact","media_download","plugin_package_preview","plugin_package_import","plugin_package_install","plugin_package_update","plugin_package_history","plugin_package_inspect","plugin_package_preview_rollback","plugin_package_rollback","connector_discover_interop","connector_preview_registration","connector_register_client","connector_metadata","connector_discover","connector_preview","connector_start","connector_status","connector_cancel","connector_save_bearer","connector_save_environment","connector_disconnect","terminal_propose","open_url","chatgpt_start","chatgpt_status","chatgpt_cancel","chatgpt_sign_out","view_stage","view_stop","artifact_get","artifact_history","artifact_submit","artifact_stop","artifact_view_open","mods","mod_example","mod_preview","mod_install","mod_toggle","mod_remove","run_list","run_get","run_activity","run_handoff","run_compact","run_context_archive","search_stats","run_events","stream_read","process_list","process_read","process_stop","process_inspect","process_dismiss","run_pause","run_resume","run_held_tests","run_held_finish","run_held_accept","run_reconcile","run_readback","upgrade_backups","upgrade_discard_backup","upgrade_retry","process_terminal","process_input_human","plan_credentials","plan_credential_save","plan_credential_delete","memory_contests","memory_resolve_contest","memory_relations","memory_forget_preview","memory_archive_export_to","memory_archive_preview_file","memory_archive_import_file","model_handoff_preview","model_handoff_approve","model_handoff_start","connector_save_environment_route","connector_send_confirm","research_sources","screen_windows","screen_session_grant","screen_sessions","screen_handoff_done","screen_session_revoke","browser_status","browser_start","browser_stop","browser_tabs","browser_open_tab","browser_profile_forget","command_catalogue","run_steer","question_answer","question_cancel","agent_list","agent_get","agent_steer","agent_cancel","job_list","job_get","job_preview","job_create","job_pause","job_preview_resume","job_resume","job_delete","notification_ack","git_capabilities","git_identity","git_status","git_diff","git_history","git_worktrees","git_inspect","git_preview_create","git_create_worktree","git_preview_remove","git_remove_worktree","git_forget_worktree","git_export","git_export_file","git_plan_propose","git_plan_receipts","git_plan_readback","agent_worktrees","agent_worktree_retire","agent_worktree_reconcile","project_recipes","environment_list","environment_plan","environment_remove","notification_status","notification_mute","goal_list","goal_get","goal_create","goal_state","goal_criterion_add","goal_criterion_edit","goal_criterion_check","goal_criterion_review","goal_step_add","goal_step_edit","goal_step_block","goal_blockers","goal_continue","export_preview","export_render","export_cancel","export_save","export_list","export_get","export_discard","telemetry_get","telemetry_set","telemetry_export","telemetry_clear","artifact_refresh","quiz_attempt","quiz_start","quiz_save","quiz_submit","quiz_share","rewind_preview","rewind_apply","context_inspect","context_compact","browser_extension_status","browser_extension_pause","browser_extension_resume","browser_extension_stop","browser_extension_unpair","storage_summary","storage_reveal","storage_delete","storage_set","storage_settings_resolve","storage_restore_chat","storage_purge_chat","storage_refresh","storage_trash_chat","storage_trash_all","storage_purge_all","storage_forget_resolve","storage_cleanup_legacy","browser_connect_status","browser_connect_install","shell_grants","shell_grant_add","shell_grant_remove","shell_undo","toolbox_list","toolbox_get","toolbox_remove","toolbox_decide","app_control_status","app_control_request","approve_simple","reject_simple","toolbox_undo_remove","remote_status","remote_set","remote_pair_start","remote_pair_cancel","remote_pair_confirm","remote_device_remove"];
    debug_assert!(ALLOWED.iter().all(|m| !NATIVE_ONLY.contains(m)));
    if !["main","pet"].contains(&window.label()) { return Err("Generated views cannot invoke native agent controls.".into()); }
    // Phase 24 (V1-108, t24-ux-settings): plain words; activity.ts isUnsupported accepts both sentences.
    if NATIVE_ONLY.contains(&method.as_str()) { return Err("Pebble can't do that from this window.".into()); }
    if !ALLOWED.contains(&method.as_str()) || !params.is_object() || params.to_string().len()>128000 { return Err("Pebble can't do that from this window.".into()); }
    // Phase 45 (D133, the person): the companion answers simple cards (approve_simple, reject_simple); approve and
    // reject stay the main window's.
    if window.label()=="pet" && !["state","new_chat","get_chat","send","cancel","open_url","attach","attach_url","attach_bytes","remove_source","model_select","model_effort","setting","set_workspace","terminal_propose","view_stage","view_stop","run_list","run_get","run_handoff","run_compact","run_context_archive","search_stats","run_events","run_pause","run_resume","run_steer","question_answer","question_cancel","command_catalogue","goal_list","artifact_get","artifact_history","artifact_refresh","quiz_attempt","quiz_start","quiz_save","quiz_submit","approve_simple","reject_simple"].contains(&method.as_str()) { return Err("Open the workspace to manage connections or review actions.".into()); }
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

/// Phase 45 (F025, F026): Tauri's standard macOS menus plus Settings…, New Conversation, Find, the command palette,
/// details, task activity, the companion and a Help menu. Each item asks the window to run the same action as its key
/// (the window ignores a menu item that repeats a key it has just handled).
fn app_menu(app:&tauri::AppHandle) -> tauri::Result<tauri::menu::Menu<tauri::Wry>> {
    use tauri::menu::{Menu,MenuItem,MenuItemKind,PredefinedMenuItem};
    let menu=Menu::default(app)?;
    let item=|id:&str,text:&str,keys:Option<&str>| MenuItem::with_id(app,id,text,true,keys);
    for kind in menu.items()? {
        let MenuItemKind::Submenu(sub)=kind else { continue };
        let name=sub.text()?;
        match name.as_str() {
            "File"=>{ sub.insert(&item("menu:new_chat","New Conversation",Some("CmdOrCtrl+N"))?,0)?; sub.insert(&PredefinedMenuItem::separator(app)?,1)?; }
            "Edit"=>{ sub.append(&PredefinedMenuItem::separator(app)?)?; sub.append(&item("menu:find","Find Conversations…",Some("CmdOrCtrl+F"))?)?; }
            "View"=>{
                sub.insert(&item("menu:palette","Command Palette…",Some("CmdOrCtrl+K"))?,0)?;
                sub.insert(&item("menu:details","Show or Hide Details",Some("CmdOrCtrl+Shift+D"))?,1)?;
                sub.insert(&item("menu:activity","Show Task Activity",Some("CmdOrCtrl+J"))?,2)?;
                sub.insert(&PredefinedMenuItem::separator(app)?,3)?;
            }
            "Window"=>{ sub.append(&PredefinedMenuItem::separator(app)?)?; sub.append(&item("menu:companion","Floating Companion",None)?)?; }
            "Help"=>{
                sub.append(&item("menu:help","Pebble Guide (Online)",None)?)?; // F080: the public guide, on the web
                sub.append(&item("menu:shortcuts","Keyboard Shortcuts",None)?)?;
                sub.append(&item("menu:whats_new","What's New in Pebble",None)?)?;
                sub.append(&PredefinedMenuItem::separator(app)?)?;
                sub.append(&item("menu:report","Report a Problem…",None)?)?;
            }
            _ if name.to_lowercase().contains("pebble")=>{
                sub.insert(&PredefinedMenuItem::separator(app)?,1)?;
                sub.insert(&item("menu:settings","Settings…",Some("CmdOrCtrl+,"))?,2)?;
            }
            _=>{}
        }
    }
    Ok(menu)
}

/// The companion runs the palette and task activity itself when it is the window in use; every other item opens the
/// workspace (unless it is already in front) and runs there.
fn menu_event(app:&tauri::AppHandle, event:tauri::menu::MenuEvent) {
    let Some(action)=event.id().as_ref().strip_prefix("menu:").map(str::to_owned) else { return };
    let pet=app.get_webview_window("pet").is_some_and(|w| w.is_focused().unwrap_or(false));
    let target=if pet && matches!(action.as_str(),"palette"|"activity") { "pet" } else { "main" };
    if target=="main" && !app.get_webview_window("main").is_some_and(|w| w.is_visible().unwrap_or(false) && w.is_focused().unwrap_or(false)) {
        reveal(app);
    }
    let _=app.emit_to(target,"pebble-menu",action);
}

// Phase 45 (F034, F056, F058; measured 2026-10-26 03:31 and in audit round R2): every way of showing the workspace runs
// reveal(), and every way back to the pebble runs pet_now(). Leaving full screen, AppKit sometimes put the workspace back
// on the desktop after pet_now had hidden it, or showed it again at the next click on the pebble, with Pebble still an
// accessory app (another app's menu bar). WORKSPACE_WANTED says whether the person asked for it last.
static WORKSPACE_WANTED: AtomicBool = AtomicBool::new(false);
// Phase 45 (D146, the person; audit round R9, F218): the workspace reopens as the person left it: full screen if it was
// full screen when it went back to the pebble, windowed if they had left full screen. The first open is full screen
// (D43). Under Stealth Lockdown the workspace is windowed anyway, so that is not taken as the person's choice.
static WORKSPACE_FULL: AtomicBool = AtomicBool::new(true);
static DOUBLE_KEY_FIXED: AtomicBool = AtomicBool::new(false); // F125: the Dock round trip, at most once per opening

fn reveal(app:&tauri::AppHandle) {
    WORKSPACE_WANTED.store(true, Ordering::SeqCst);
    DOUBLE_KEY_FIXED.store(false, Ordering::SeqCst);
    // Phase 45 (F058): already active as an accessory app (another app's menu bar) before becoming an ordinary one.
    #[cfg(target_os="macos")]
    let handoff=!stealth_on() && !regular_policy() && app_active();
    // Phase 33: under Stealth Lockdown the workspace stays out of the Dock and ⌘Tab and does not take focus.
    #[cfg(target_os="macos")]
    if !stealth_on() { let _=app.set_activation_policy(tauri::ActivationPolicy::Regular); }
    #[cfg(target_os="macos")]
    if !stealth_on() { companion_over_full_screen(app); } // Phase 39 (D78): re-applied after the policy change; under
    // Stealth the policy does not change here, and re-applying during the desktop anchor's Space switch (D82) left the
    // companion off the desktop (diagnostic session 2)
    if let Some(window) = app.get_webview_window("main") { let _=window.show(); let _=window.unminimize(); focus(&window); web_first_responder(&window); }
    #[cfg(target_os="macos")]
    if handoff { menu_bar_handoff(app); }
    keyboard_to_workspace_later(app);
}

/// Phase 45 (F101, F038; measured on the 1.9.6 build, `native/s3c-1.9.6.jsonl`): shown or given the keyboard, the
/// workspace's window itself was the first responder, not its web view, so keys went to AppKit: Escape took the
/// full-screen workspace out of full screen (the page, which keeps every Escape, never saw it) and the window's own
/// keys did nothing until a click. Its web view becomes the first responder, unless it already is.
fn web_first_responder(window:&tauri::WebviewWindow) {
    #[cfg(target_os="macos")]
    {
        let Ok(ns)=window.ns_window() else { return };
        let ns=ns as usize;
        let _=window.with_webview(move |webview| unsafe {
            let view=webview.inner() as *mut objc2::runtime::AnyObject;
            let ns=ns as *mut objc2::runtime::AnyObject;
            if view.is_null() || ns.is_null() { return; }
            let current: *mut objc2::runtime::AnyObject = objc2::msg_send![ns, firstResponder];
            if current != view {
                let _: objc2::runtime::Bool = objc2::msg_send![ns, makeFirstResponder: view];
            }
        });
    }
    #[cfg(not(target_os="macos"))]
    let _=window;
}

/// Phase 45 (measured 2026-10-26 06:24 on the 1.9.5 build, `native/s3b-1.9.5.jsonl`): opened by a click in the companion
/// (which, a non-activating panel since 1.9.5, no longer activates Pebble), the workspace came forward but the companion
/// kept the keyboard: ⌘W and typing went to the collapsed panel (1.9.4, whose click activated Pebble, did not). Shortly
/// after the workspace is shown, and again as its full-screen move settles, it takes the keyboard when the companion
/// holds it; the pebble's pass-through (F085) is decided again at the same moments, once focus has settled.
fn keyboard_to_workspace_later(app:&tauri::AppHandle) {
    let later=app.clone();
    let _=std::thread::Builder::new().name("pebble-workspace-key".into()).spawn(move || {
        for wait in [250u64, 700, 1500] {
            std::thread::sleep(Duration::from_millis(wait));
            let main_thread=later.clone();
            let _=later.run_on_main_thread(move || {
                if !WORKSPACE_WANTED.load(Ordering::SeqCst) { return; }
                let (Some(main), Some(pet))=(main_thread.get_webview_window("main"), main_thread.get_webview_window("pet")) else { return };
                if main.is_visible().unwrap_or(false) && !main.is_focused().unwrap_or(false) && pet.is_focused().unwrap_or(false) {
                    focus(&main);
                    diag::log("workspace_keyboard", json!({"from": "companion"}));
                } else if main.is_visible().unwrap_or(false) && main.is_focused().unwrap_or(false) && pet.is_focused().unwrap_or(false)
                    && !stealth_on() && !DOUBLE_KEY_FIXED.swap(true, Ordering::SeqCst) {
                    // Phase 45 (F125, audit round R4; native/s3d-finder-1.9.7.jsonl): the workspace already full screen
                    // on its own Space, opened from the companion over another app: both windows report key while the
                    // window server keeps the companion's keyboard (typing, Escape and ⌃⌘F lost until Pebble is
                    // re-activated). The Dock round trip re-activates Pebble with the workspace key.
                    diag::log("workspace_keyboard", json!({"from": "companion", "both_key": true}));
                    #[cfg(target_os="macos")]
                    menu_bar_handoff(&main_thread);
                }
                if main.is_focused().unwrap_or(false) { web_first_responder(&main); }
                pet_passthrough(&main_thread, true);
            });
        }
    });
}

/// set_focus activates Pebble; under Stealth Lockdown the window is only brought forward and made key, which a
/// non-activating panel takes without activating the app. Phase 38 (1.5.1): the companion is always such a panel
/// (companion_over_full_screen), so it never activates Pebble either: the app you are in stays in front.
fn focus(window:&tauri::WebviewWindow) {
    #[cfg(target_os="macos")]
    if stealth_on() || window.label()=="pet" {
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
    diag::log("to_pet", json!({}));
    if !stealth_on() && app.get_webview_window("main").map(|w| w.is_visible().unwrap_or(false)).unwrap_or(false) {
        WORKSPACE_FULL.store(main_fullscreen(app), Ordering::SeqCst); // D146: how the person left it
    }
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
    WORKSPACE_WANTED.store(false, Ordering::SeqCst);
    pet_passthrough(app, false); // Phase 45 (F085): the pebble takes clicks again
    if let Some(w)=app.get_webview_window("pet") { let _=w.show(); }
    if let Some(w)=app.get_webview_window("main") { let _=w.hide(); }
    // Phase 45 (F056): a second look once the exit from full screen has settled hides it again if AppKit brought it
    // back, unless the workspace was asked for meanwhile.
    let later=app.clone();
    let _=std::thread::Builder::new().name("pebble-pet-recheck".into()).spawn(move || {
        std::thread::sleep(Duration::from_millis(700));
        let main_thread=later.clone();
        let _=later.run_on_main_thread(move || {
            if WORKSPACE_WANTED.load(Ordering::SeqCst) { return; }
            if let Some(w)=main_thread.get_webview_window("main") {
                if w.is_visible().unwrap_or(false) { let _=w.hide(); diag::log("workspace_rehidden", json!({"why": "after pet_now"})); }
            }
        });
    });
    #[cfg(target_os="macos")]
    yield_on_this_space(); // Phase 45 (F081): the next active app is one on this Space, never another app's full screen
    #[cfg(target_os="macos")]
    let _=app.set_activation_policy(tauri::ActivationPolicy::Accessory);
    #[cfg(target_os="macos")]
    companion_over_full_screen(app); // Phase 39 (D78): re-applied after the switch to Accessory
}

/// The main window's title bar follows full screen: the standard (opaque) bar in full screen, where macOS keeps an
/// overlay bar's strip hidden and its buttons with it, and the overlay bar otherwise.
fn sync_title_bar(window:&tauri::Window) {
    #[cfg(target_os="macos")]
    if window.label()=="main" {
        let full=appkit_fullscreen(window.ns_window());
        if MAIN_FULLSCREEN.swap(full, Ordering::SeqCst)!=full {
            diag::log("title_bar_style", json!({"full_screen": full}));
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

/// Phase 45 (F034): whether AppKit runs Pebble as an ordinary app right now (NSApplicationActivationPolicyRegular).
#[cfg(target_os="macos")]
fn regular_policy() -> bool {
    use objc2::{msg_send, runtime::{AnyClass, AnyObject}};
    let Some(class)=AnyClass::get(c"NSApplication") else { return true };
    unsafe {
        let app: *mut AnyObject = msg_send![class, sharedApplication];
        let policy: isize = msg_send![app, activationPolicy];
        policy == 0
    }
}

/// Phase 45 (F058): whether Pebble is the active app now (main thread).
#[cfg(target_os="macos")]
fn app_active() -> bool {
    use objc2::{msg_send, runtime::{AnyClass, AnyObject, Bool}};
    let Some(class)=AnyClass::get(c"NSApplication") else { return false };
    unsafe {
        let app: *mut AnyObject = msg_send![class, sharedApplication];
        let active: Bool = msg_send![app, isActive];
        active.as_bool()
    }
}

/// Activates another running app by bundle identifier or process number (main thread); false when it is not running.
#[cfg(target_os="macos")]
fn activate_other(bundle:Option<&str>, pid:Option<i32>) -> bool {
    use objc2::{msg_send, runtime::{AnyClass, AnyObject, Bool}};
    let Some(running)=AnyClass::get(c"NSRunningApplication") else { return false };
    unsafe {
        let target: *mut AnyObject = match (bundle, pid) {
            (Some(id), _) => {
                let id=objc2_foundation::NSString::from_str(id);
                let list: *mut AnyObject = msg_send![running, runningApplicationsWithBundleIdentifier: &*id];
                if list.is_null() { std::ptr::null_mut() } else { msg_send![list, firstObject] }
            }
            (None, Some(pid)) => msg_send![running, runningApplicationWithProcessIdentifier: pid],
            _ => std::ptr::null_mut(),
        };
        if target.is_null() { return false; }
        let done: Bool = msg_send![target, activateWithOptions: 0usize];
        done.as_bool()
    }
}

/// Phase 45 (F058; measured 2026-10-26 05:06 on a stand-in, `.runtime/phase45-dev/native/menubar_policy.py`): an app
/// already active as an accessory when it becomes an ordinary one keeps another app's menu bar, even when it activates
/// itself again or deactivates and activates; activating the Dock and then Pebble 0.2 s later gave it its menu bar. (An
/// inactive accessory that becomes ordinary and activates gets it at once, as when the companion's "Open full
/// workspace" is pressed now that the companion never activates Pebble.)
#[cfg(target_os="macos")]
fn menu_bar_handoff(app:&tauri::AppHandle) {
    if !activate_other(Some("com.apple.dock"), None) { return; }
    diag::log("menu_bar_handoff", json!({}));
    let later=app.clone();
    let _=std::thread::Builder::new().name("pebble-menu-bar".into()).spawn(move || {
        std::thread::sleep(Duration::from_millis(200));
        let main_thread=later.clone();
        let _=later.run_on_main_thread(move || {
            if !WORKSPACE_WANTED.load(Ordering::SeqCst) || stealth_on() { return; }
            if let Some(w)=main_thread.get_webview_window("main") { focus(&w); }
        });
    });
}

#[cfg(target_os="macos")]
#[link(name = "CoreGraphics", kind = "framework")]
extern "C" { fn CGWindowListCopyWindowInfo(option: u32, relative_to: u32) -> *mut objc2::runtime::AnyObject; }

/// Phase 45 (F036, F081; audit round R2): when Pebble stops being an ordinary app while it is the active one (the red
/// button, ⌘W, Floating companion, Stealth Lockdown on), macOS chooses the next active app itself, sometimes one in a
/// full-screen Space elsewhere, and follows it there. So the app owning the frontmost ordinary window on this Space (the
/// window server's on-screen list is this Space's, front to back) is made active first, or Finder when there is none.
/// Main thread.
#[cfg(target_os="macos")]
fn yield_on_this_space() {
    use objc2::{msg_send, runtime::AnyObject};
    use objc2_foundation::NSString;
    if !app_active() { return; }
    const ON_SCREEN_ONLY: u32 = 1 << 0;
    const EXCLUDE_DESKTOP: u32 = 1 << 4;
    const OVERLAYS: [&str; 3] = ["Dock", "Window Server", "WindowManager"];
    let me=std::process::id() as i64;
    let mut chosen: Option<i32> = None;
    unsafe {
        let list = CGWindowListCopyWindowInfo(ON_SCREEN_ONLY | EXCLUDE_DESKTOP, 0);
        if !list.is_null() {
            let number = |info: *mut AnyObject, key: &str| -> Option<f64> {
                let value: *mut AnyObject = msg_send![info, objectForKey: &*NSString::from_str(key)];
                if value.is_null() { None } else { let v: f64 = msg_send![value, doubleValue]; Some(v) }
            };
            let count: usize = msg_send![list, count];
            for index in 0..count {
                let info: *mut AnyObject = msg_send![list, objectAtIndex: index];
                let (Some(layer), Some(pid)) = (number(info, "kCGWindowLayer"), number(info, "kCGWindowOwnerPID")) else { continue };
                if layer != 0.0 || pid as i64 == me || number(info, "kCGWindowAlpha").unwrap_or(1.0) <= 0.0 { continue; }
                let owner: *mut AnyObject = msg_send![info, objectForKey: &*NSString::from_str("kCGWindowOwnerName")];
                let name = if owner.is_null() { String::new() } else { (*(owner as *const NSString)).to_string() };
                if OVERLAYS.contains(&name.as_str()) { continue; }
                let bounds: *mut AnyObject = msg_send![info, objectForKey: &*NSString::from_str("kCGWindowBounds")];
                if !bounds.is_null() {
                    let side = |key: &str| -> f64 {
                        let v: *mut AnyObject = msg_send![bounds, objectForKey: &*NSString::from_str(key)];
                        if v.is_null() { 0.0 } else { msg_send![v, doubleValue] }
                    };
                    if side("Width") < 80.0 || side("Height") < 60.0 { continue; }
                }
                chosen = Some(pid as i32);
                break;
            }
            let _: () = msg_send![list, release];
        }
    }
    let done = chosen.is_some_and(|pid| activate_other(None, Some(pid))) || activate_other(Some("com.apple.finder"), None);
    diag::log("yielded_on_this_space", json!({"to_pid": chosen, "done": done}));
}

#[cfg(target_os="macos")]
mod stealth_mac {
    use objc2::{ffi, msg_send, runtime::{AnyClass, AnyObject, Bool, Imp, Sel}, sel};
    use std::collections::HashMap;
    use std::ffi::c_void;
    use std::sync::{Mutex, OnceLock};

    const NONACTIVATING_PANEL: usize = 1 << 7; // NSWindowStyleMaskNonactivatingPanel: honoured only by an NSPanel
    const MANAGED: usize = 1 << 2; // NSWindowCollectionBehaviorManaged: shown in Mission Control
    const TRANSIENT: usize = 1 << 3; // NSWindowCollectionBehaviorTransient: left out of Mission Control
    static BEHAVIOR: Mutex<Option<HashMap<String, usize>>> = Mutex::new(None);
    // D86 (Phase 43): the class each swapped window had before it became a panel (TaoWindow, or AppKit's key-value
    // observing subclass of it), by window, so set_panel puts that very class back when lockdown ends.
    static ORIGINAL: Mutex<Option<HashMap<usize, usize>>> = Mutex::new(None);
    // What key-value observing itself gives an observed object's class: its own bookkeeping, never a setter. A subclass
    // that also overrides an observed key's setter would lose those notifications while swapped, so it is refused.
    const KVO_OWN: [&str; 3] = ["class", "dealloc", "_isKVOA"];
    const KVO_PANEL: bool = true;  // D86 (Phase 43): tao's KVO subclass may become the panel; measured natively (D81)
    // D86 (Phase 45, measured 2026-10-26 02:17 in the shell diagnostic): both of Pebble's windows are observed for these
    // setters (their KVO subclasses override setContentView:, and setOpaque: on one), which kept every swap refused.
    // The panel carries them itself and posts the same will/did change notifications around NSWindow's own setter, so
    // the observers (registered on the object, not its class) hear every change while the window is a panel.
    const NOTIFIED: [(&str, &str); 2] = [("setContentView:", "contentView"), ("setOpaque:", "opaque")];

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
            // D86 (Phase 45): the observed setters, with their notifications (NOTIFIED).
            type SetObject = extern "C-unwind" fn(*mut AnyObject, Sel, *mut AnyObject);
            type SetFlag = extern "C-unwind" fn(*mut AnyObject, Sel, Bool);
            let setters: [(Sel, Imp); 2] = [
                (sel!(setContentView:), std::mem::transmute(set_content_view as SetObject)),
                (sel!(setOpaque:), std::mem::transmute(set_opaque as SetFlag))];
            for (name, imp) in setters {
                let method = ffi::class_getInstanceMethod(panel, name);
                if method.is_null() || !ffi::class_addMethod(cls, name, imp, ffi::method_getTypeEncoding(method)).as_bool() {
                    return None;
                }
            }
            // D86 (Phase 43): key-value observing registrations run under the window's own class (as_original).
            let object = AnyClass::get(c"NSObject")?;
            type Remove = extern "C-unwind" fn(*mut AnyObject, Sel, *mut AnyObject, *mut AnyObject);
            type RemoveContext = extern "C-unwind" fn(*mut AnyObject, Sel, *mut AnyObject, *mut AnyObject, *mut c_void);
            type Add = extern "C-unwind" fn(*mut AnyObject, Sel, *mut AnyObject, *mut AnyObject, usize, *mut c_void);
            let observing: [(Sel, Imp); 3] = [
                (sel!(removeObserver:forKeyPath:), std::mem::transmute(remove_observer as Remove)),
                (sel!(removeObserver:forKeyPath:context:), std::mem::transmute(remove_observer_context as RemoveContext)),
                (sel!(addObserver:forKeyPath:options:context:), std::mem::transmute(add_observer as Add))];
            for (name, imp) in observing {
                let method = ffi::class_getInstanceMethod(object, name);
                if method.is_null() || !ffi::class_addMethod(cls, name, imp, ffi::method_getTypeEncoding(method)).as_bool() {
                    return None;
                }
            }
            ffi::objc_registerClassPair(cls);
            let offset = |c: *const AnyClass| ffi::ivar_getOffset(ffi::class_getInstanceVariable(c, c"focusable".as_ptr()));
            if offset(cls) != offset(tao) || ffi::class_getInstanceSize(cls) > ffi::class_getInstanceSize(tao) { return None; }
            Some(cls as usize)
        });
        made.map(|ptr| unsafe { &*(ptr as *const AnyClass) })
    }

    /// D86 (Phase 43): whether a window of this class may become the panel. tao's windows are observed (key-value
    /// observing) before Pebble's setup runs, so AppKit has already given each its NSKVONotifying_TaoWindow subclass and
    /// the old exact-TaoWindow check never swapped one: Stealth Lockdown kept no window from taking focus. That subclass
    /// may be swapped too when it overrides nothing but KVO's own bookkeeping (KVO_OWN): then no automatically notified
    /// setter is lost while the window is a panel, AppKit's own will/didChange notifications still reach the observers
    /// (they belong to the object, not to its class), and set_panel restores that same subclass afterwards. Err names
    /// what stands in the way, for the shell diagnostic.
    fn swappable(class: *const AnyClass, tao: &AnyClass) -> Result<(), Vec<String>> {
        if class == tao as *const AnyClass {
            return Ok(());
        }
        let Some(class) = (unsafe { class.as_ref() }) else { return Err(Vec::new()) };
        let name = class.name().to_string_lossy().into_owned();
        // D86's way back: KVO_PANEL false (or PEBBLE_STEALTH_KVO_PANEL=0 for one launch) keeps 1.8.0's exact check.
        if !KVO_PANEL || std::env::var("PEBBLE_STEALTH_KVO_PANEL").is_ok_and(|v| v == "0") {
            return Err(vec![name]);
        }
        if !name.starts_with("NSKVONotifying_") || !class.superclass().is_some_and(|s| std::ptr::eq(s, tao)) {
            return Err(vec![name]);
        }
        let extra: Vec<String> = class.instance_methods().iter().map(|m| m.name().name().to_string_lossy().into_owned())
            .filter(|n| !KVO_OWN.contains(&n.as_str()) && !NOTIFIED.iter().any(|(setter, _)| setter == n)).collect();
        if extra.is_empty() { Ok(()) } else { Err(extra) }
    }

    fn class_name(class: *const AnyClass) -> String {
        unsafe { class.as_ref() }.map(|c| c.name().to_string_lossy().into_owned()).unwrap_or_default()
    }

    /// D86 (Phase 45): NSWindow's own implementation of an observed setter, between the change notifications its KVO
    /// subclass would have posted (NOTIFIED).
    fn notified(this: *mut AnyObject, sel: Sel, call: impl FnOnce(Imp)) {
        let Some(key) = NOTIFIED.iter().find(|(setter, _)| sel.name().to_bytes() == setter.as_bytes()).map(|(_, k)| *k) else { return };
        let Some(panel) = AnyClass::get(c"NSPanel") else { return };
        let key = objc2_foundation::NSString::from_str(key);
        unsafe {
            let Some(imp) = ffi::method_getImplementation(ffi::class_getInstanceMethod(panel, sel)) else { return };
            let _: () = msg_send![this, willChangeValueForKey: &*key];
            call(imp);
            let _: () = msg_send![this, didChangeValueForKey: &*key];
        }
    }

    extern "C-unwind" fn set_content_view(this: *mut AnyObject, sel: Sel, view: *mut AnyObject) {
        notified(this, sel, |imp| unsafe {
            let call: extern "C-unwind" fn(*mut AnyObject, Sel, *mut AnyObject) = std::mem::transmute(imp);
            call(this, sel, view)
        });
    }

    extern "C-unwind" fn set_opaque(this: *mut AnyObject, sel: Sel, opaque: Bool) {
        notified(this, sel, |imp| unsafe {
            let call: extern "C-unwind" fn(*mut AnyObject, Sel, Bool) = std::mem::transmute(imp);
            call(this, sel, opaque)
        });
    }

    // D86 (Phase 43; measured on a stand-in window, `.runtime/phase43-dev/m3-integration.md`): key-value observing finds
    // an observance through the object's class, so removing an observer while the window was the panel threw
    // NSRangeException, and adding one would hang KVO's own subclass on the panel. On the panel these three run with
    // the window's original class in place; the panel comes back afterwards unless KVO now overrides a setter there.
    extern "C-unwind" fn remove_observer(this: *mut AnyObject, sel: Sel, observer: *mut AnyObject, key: *mut AnyObject) {
        as_original(this, sel, |imp| unsafe {
            let call: extern "C-unwind" fn(*mut AnyObject, Sel, *mut AnyObject, *mut AnyObject) = std::mem::transmute(imp);
            call(this, sel, observer, key)
        });
    }

    extern "C-unwind" fn remove_observer_context(this: *mut AnyObject, sel: Sel, observer: *mut AnyObject, key: *mut AnyObject,
                                                 context: *mut c_void) {
        as_original(this, sel, |imp| unsafe {
            let call: extern "C-unwind" fn(*mut AnyObject, Sel, *mut AnyObject, *mut AnyObject, *mut c_void) = std::mem::transmute(imp);
            call(this, sel, observer, key, context)
        });
    }

    extern "C-unwind" fn add_observer(this: *mut AnyObject, sel: Sel, observer: *mut AnyObject, key: *mut AnyObject, options: usize,
                                      context: *mut c_void) {
        as_original(this, sel, |imp| unsafe {
            let call: extern "C-unwind" fn(*mut AnyObject, Sel, *mut AnyObject, *mut AnyObject, usize, *mut c_void) = std::mem::transmute(imp);
            call(this, sel, observer, key, options, context)
        });
    }

    /// Runs `call` with the window's original class in place, given that class's own implementation of `sel`.
    fn as_original(ns: *mut AnyObject, sel: Sel, call: impl FnOnce(Imp)) {
        let Some(tao) = AnyClass::get(c"TaoWindow") else { return };
        let saved = ORIGINAL.lock().unwrap().as_ref().and_then(|m| m.get(&(ns as usize)).copied());
        let original = saved.map_or(tao as *const AnyClass, |c| c as *const AnyClass);
        unsafe {
            let panel = ffi::object_getClass(ns);
            ffi::object_setClass(ns, original);
            let Some(imp) = ffi::method_getImplementation(ffi::class_getInstanceMethod(original, sel)) else {
                ffi::object_setClass(ns, panel);
                return;
            };
            call(imp);
            let now = ffi::object_getClass(ns);
            let keep = swappable(now, tao).is_ok();
            {
                let mut saved = ORIGINAL.lock().unwrap();  // never held while AppKit runs (observers may call back)
                let saved = saved.get_or_insert_with(HashMap::new);
                if keep { saved.insert(ns as usize, now as usize); } else { saved.remove(&(ns as usize)); }
            }
            if keep {
                ffi::object_setClass(ns, panel);
            } else {
                // A setter of the window's is observed now: it stays its own class and only stops being non-activating.
                let mask: usize = msg_send![ns, styleMask];
                let _: () = msg_send![ns, setStyleMask: mask & !NONACTIVATING_PANEL];
                prevent_activation(ns, false);
                super::diag::log("stealth_panel_kept", serde_json::json!({"class": class_name(now),
                    "after": sel.name().to_string_lossy()}));
            }
        }
    }

    /// Phase 45 (F075, F076; measured 2026-10-26 05:05 on a stand-in, `.runtime/phase45-dev/native/panel_activation.py`):
    /// a window made a non-activating panel after its creation (the class swap, then the style bit) still activated its
    /// app at a click, which made Pebble the active app over the person's app, Stealth Lockdown or not. NSPanel sets the
    /// window server's prevents-activation tag only when a panel is created non-activating; set here too, a click no
    /// longer activated the app (as with a panel created that way), while typing and ⌘V still reached a web view's text
    /// box in it (panel_typing.py). A runtime without the method keeps the style bit alone.
    fn prevent_activation(ns: *mut AnyObject, on: bool) {
        unsafe {
            let responds: Bool = msg_send![ns, respondsToSelector: sel!(_setPreventsActivation:)];
            if responds.as_bool() { let _: () = msg_send![ns, _setPreventsActivation: Bool::new(on)]; }
        }
    }

    /// Lockdown for one window: a non-activating panel while on, the window's own class again when off (D86: that
    /// class may be AppKit's key-value observing subclass of TaoWindow). Returns whether the window is a panel now.
    pub fn set_panel(ns: *mut AnyObject, on: bool) -> bool {
        let (Some(tao), Some(panel)) = (AnyClass::get(c"TaoWindow"), panel_class()) else { return false };
        unsafe {
            let class = ffi::object_getClass(ns);
            let mask: usize = msg_send![ns, styleMask];
            let descends = |mut c: *const AnyClass| {
                while !c.is_null() && c != panel as *const AnyClass { c = ffi::class_getSuperclass(c); }
                !c.is_null()
            };
            if on && !descends(class) {
                match swappable(class, tao) {
                    Ok(()) => {
                        ORIGINAL.lock().unwrap().get_or_insert_with(HashMap::new).insert(ns as usize, class as usize);
                        ffi::object_setClass(ns, panel);
                        let _: () = msg_send![ns, setStyleMask: mask | NONACTIVATING_PANEL];
                        prevent_activation(ns, true);
                        let _: () = msg_send![ns, setBecomesKeyOnlyIfNeeded: Bool::NO];
                        super::diag::log("stealth_panel", serde_json::json!({"on": true, "from": class_name(class)}));
                    }
                    Err(blocking) => super::diag::log("stealth_panel_refused", serde_json::json!({
                        "class": class_name(class), "blocking": blocking})),
                }
            } else if !on && class == panel as *const AnyClass {
                let saved = ORIGINAL.lock().unwrap().get_or_insert_with(HashMap::new).remove(&(ns as usize));
                let tao = saved.map_or(tao as *const AnyClass, |c| c as *const AnyClass);
                let _: () = msg_send![ns, setStyleMask: mask & !NONACTIVATING_PANEL];
                prevent_activation(ns, false);
                ffi::object_setClass(ns, tao);
                super::diag::log("stealth_panel", serde_json::json!({"on": false, "to": class_name(tao)}));
            } else if !on && descends(class) {
                // Observed while it was a panel: KVO now sits on the panel with its own subclass, which must stay; the
                // window keeps that class and only stops being non-activating.
                let _: () = msg_send![ns, setStyleMask: mask & !NONACTIVATING_PANEL];
                prevent_activation(ns, false);
                super::diag::log("stealth_panel_kept", serde_json::json!({"class": class_name(class)}));
            }
            descends(ffi::object_getClass(ns))
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

    /// Phase 39 (the person's report): the first time a full-screen workspace became a stealth window, its red, yellow
    /// and green buttons stayed hidden. AppKit hides the title bar and its buttons in full screen and shows them again
    /// as the exit ends; the class and style change right after can leave them hidden, so they are shown explicitly:
    /// the three standard buttons and every view between each and the window's frame view (the title bar's views; how
    /// many there are differs between macOS versions), at most 8 levels.
    pub fn show_window_buttons(ns: *mut AnyObject) {
        unsafe {
            let content: *mut AnyObject = msg_send![ns, contentView];
            let frame_view: *mut AnyObject = if content.is_null() { std::ptr::null_mut() } else { msg_send![content, superview] };
            for kind in [0usize, 1, 2] { // NSWindowCloseButton, NSWindowMiniaturizeButton, NSWindowZoomButton
                let mut view: *mut AnyObject = msg_send![ns, standardWindowButton: kind];
                for _ in 0..8 {
                    if view.is_null() || view == frame_view { break; }
                    let _: () = msg_send![view, setHidden: Bool::NO];
                    let _: () = msg_send![view, setAlphaValue: 1.0f64];
                    view = msg_send![view, superview];
                }
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
    diag::log("apply_stealth", json!({"on": on}));
    diag::latency(app, 2500, "stealth");
    STEALTH.store(on, Ordering::SeqCst);
    let mut deferred=false; // Phase 39 (D83): the Accessory switch waits for the workspace's full-screen exit
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
            deferred |= leaving;
        }
    }
    #[cfg(target_os="macos")]
    {
        stealth_mac::set_sharing_all(on);
        let workspace_open=app.get_webview_window("main").and_then(|w| w.is_visible().ok()).unwrap_or(false);
        // Phase 39 (D83): measured on the person's Mac (diagnostic session 1), switching to Accessory while the
        // workspace leaves full screen deactivated Pebble and macOS held the exit until it was active again (still in
        // full screen 5 s later), so the switch waits for the exit (windowed_after_full_screen applies it).
        if on && !deferred { yield_on_this_space(); } // Phase 45 (F036): Stealth on never moves the person to another Space
        if !deferred {
            let _=app.set_activation_policy(if on || !workspace_open {tauri::ActivationPolicy::Accessory} else {tauri::ActivationPolicy::Regular});
        }
    }
    // Back to full screen once Pebble is an ordinary app again (Dock and ⌘Tab), as the workspace was before stealth.
    if let Some(main)=app.get_webview_window("main") {
        if RETURN_TO_FULL_SCREEN.swap(false, Ordering::SeqCst) { let _=main.set_fullscreen(true); }
    }
    // Phase 38: turning stealth off made every window an ordinary one again; the companion stays a panel.
    #[cfg(target_os="macos")]
    companion_over_full_screen(app);
    tray_state(app, on);
    diag::snapshot(app, "apply_stealth end");
    diag::snapshots_after(app, "apply_stealth", &[100, 300, 600, 1000, 2500]);
}

/// Phase 38 (1.5.1, the person's request): the floating companion stays on screen with the person over every app,
/// full-screen ones included, like a genie. Joining every Space (configure_pet) is not enough for another app's
/// full-screen Space: macOS shows only a non-activating panel there, so the companion is always one, the same panel
/// Stealth Lockdown makes, with or without stealth. It never hides when Pebble is not the active app, and it never
/// activates Pebble (focus), so the full-screen app stays in front. Returns whether the companion is a panel (false
/// only when the runtime layout does not allow the swap; it then stays an ordinary floating window).
///
/// Phase 39 (1.5.2, D78): 1.5.1 made the companion a panel but it still stayed out of other apps' full-screen Spaces
/// on the person's Mac. Measured on macOS 27 (`.runtime/phase39-fsprobe`): the window server decides a window's place
/// in other apps' full-screen Spaces when its Space behaviour is set, from what the window and the app are at that
/// moment. Set at launch, while the companion was still an ordinary window of an ordinary app, that decision kept it
/// out even after it became a panel of an accessory app; set again afterwards, it put the companion in, for an
/// accessory app and (as a panel) for a regular one too, while the workspace is open. So the behaviour is re-applied,
/// unchanged, after the panel switch here, and every caller runs this after each activation-policy change (launch,
/// opening the workspace, returning to the companion, Stealth Lockdown).
#[cfg(target_os="macos")]
fn companion_over_full_screen(app:&tauri::AppHandle) -> bool {
    let Some(pet)=app.get_webview_window("pet") else { return false };
    let Ok(ns)=pet.ns_window() else { return false };
    let ns=ns as *mut objc2::runtime::AnyObject;
    let panel=stealth_mac::set_panel(ns, true);
    unsafe {
        let _: () = objc2::msg_send![ns, setHidesOnDeactivate: objc2::runtime::Bool::NO];
        let behaviour: usize = objc2::msg_send![ns, collectionBehavior];
        let _: () = objc2::msg_send![ns, setCollectionBehavior: 0usize];
        let _: () = objc2::msg_send![ns, setCollectionBehavior: behaviour];
    }
    panel
}

static LEFT_FULL_SCREEN: AtomicBool = AtomicBool::new(false); // stealth took the workspace out of full screen
static RETURN_TO_FULL_SCREEN: AtomicBool = AtomicBool::new(false);

/// Phase 45 (F177, measured in native/f177-1.9.10.jsonl): with another app active, the companion's message box keeps
/// the keyboard (by design, F146), so ⌘Q quit Pebble, and with it any running task, while the menu bar named the
/// other app; ⌘H and ⌥⌘H would hide Pebble or every other app the same way. While the menu bar is another app's those
/// keys are dropped. The Dock's Quit, the menu-bar icon's Quit and a logout still quit (none of them is a key event).
#[cfg(target_os="macos")]
mod quit_guard {
    use block2::RcBlock;
    use objc2::{msg_send, rc::Retained, runtime::{AnyClass, AnyObject}};
    use serde_json::json;
    use std::ptr::NonNull;

    const KEY_DOWN: u64 = 1 << 10; // NSEventMaskKeyDown
    const SHIFT: usize = 1 << 17;
    const CONTROL: usize = 1 << 18;
    const OPTION: usize = 1 << 19;
    const COMMAND: usize = 1 << 20;
    const KEY_H: u16 = 4;
    const KEY_Q: u16 = 12;

    /// Whether a key press is one Pebble must not act on while it is not the active app.
    pub fn guarded(code: u16, flags: usize) -> bool {
        let mods = flags & (SHIFT | CONTROL | OPTION | COMMAND);
        (code == KEY_Q && mods == COMMAND) || (code == KEY_H && (mods == COMMAND || mods == COMMAND | OPTION))
    }

    /// Whether macOS shows Pebble's menu bar. Measured on 1.9.11's first build (native/f177-1.9.11.jsonl): after a click
    /// in the companion's box NSApp.isActive is true while the menu bar still belongs to Finder, so "active" is not the
    /// question; the owner of the menu bar is.
    fn menu_bar_ours() -> bool {
        let Some(class) = AnyClass::get(c"NSWorkspace") else { return true };
        unsafe {
            let workspace: *mut AnyObject = msg_send![class, sharedWorkspace];
            if workspace.is_null() { return true; }
            let owner: *mut AnyObject = msg_send![workspace, menuBarOwningApplication];
            if owner.is_null() { return true; }
            let pid: i32 = msg_send![owner, processIdentifier];
            pid == std::process::id() as i32
        }
    }

    /// Installs the local key monitor once, for the app's lifetime (main thread).
    pub fn install() {
        let Some(class) = AnyClass::get(c"NSEvent") else { return };
        let block = RcBlock::new(|event: NonNull<AnyObject>| -> *mut AnyObject {
            let (code, flags): (u16, usize) = unsafe { (msg_send![event.as_ref(), keyCode], msg_send![event.as_ref(), modifierFlags]) };
            if guarded(code, flags) && !menu_bar_ours() {
                super::diag::log("key_equivalent_dropped", json!({"key": if code == KEY_Q { "cmd+q" } else { "cmd+h" }}));
                return std::ptr::null_mut();
            }
            event.as_ptr()
        });
        let monitor: Option<Retained<AnyObject>> = unsafe { msg_send![class, addLocalMonitorForEventsMatchingMask: KEY_DOWN, handler: &*block] };
        std::mem::forget(monitor); // AppKit keeps the monitor until it is removed; it never is
    }
}

/// Phase 39 (D83): a one-shot observer of the workspace's NSWindowDidExitFullScreenNotification (main thread only).
#[cfg(target_os="macos")]
mod exit_observer {
    use block2::RcBlock;
    use objc2::{msg_send, rc::Retained, runtime::{AnyClass, AnyObject}};
    use objc2_foundation::NSString;
    use std::{cell::RefCell, sync::mpsc};
    use tauri::Manager;

    thread_local! { static TOKEN: RefCell<Option<Retained<AnyObject>>> = const { RefCell::new(None) }; }

    pub fn watch(app: &tauri::AppHandle, ended: mpsc::Sender<()>) {
        stop();
        let Some(main) = app.get_webview_window("main") else { return };
        let Ok(ns) = main.ns_window() else { return };
        let Some(center_class) = AnyClass::get(c"NSNotificationCenter") else { return };
        unsafe {
            let window: &AnyObject = &*(ns as *const AnyObject);
            let center: Option<Retained<AnyObject>> = msg_send![center_class, defaultCenter];
            let Some(center) = center else { return };
            let block = RcBlock::new(move |_note: std::ptr::NonNull<AnyObject>| { let _ = ended.send(()); });
            let token: Option<Retained<AnyObject>> = msg_send![&*center, addObserverForName: &*NSString::from_str("NSWindowDidExitFullScreenNotification"),
                object: Some(window), queue: Option::<&AnyObject>::None, usingBlock: &*block];
            TOKEN.with(|slot| *slot.borrow_mut() = token);
        }
    }

    pub fn stop() {
        let Some(center_class) = AnyClass::get(c"NSNotificationCenter") else { return };
        TOKEN.with(|slot| {
            if let Some(token) = slot.borrow_mut().take() {
                unsafe {
                    let center: Option<Retained<AnyObject>> = msg_send![center_class, defaultCenter];
                    if let Some(center) = center { let _: () = msg_send![&*center, removeObserver: &*token]; }
                }
            }
        });
    }
}

/// Once the workspace has left its full-screen Space (as to_pet waits for it), and only if stealth is still on: an
/// ordinary window, the configured size within 80 % of the screen, centred, then the non-activating panel.
#[cfg(target_os="macos")]
fn windowed_after_full_screen(app:&tauri::AppHandle) {
    // Phase 39 (D83, measured on the person's Mac in diagnostic session 2): AppKit clears the full-screen flag as the
    // exit begins (about 35 ms in), not when it ends, so 1.5.0 windowed the workspace mid-animation and its resize was
    // a second jump. The end of the exit is AppKit's NSWindowDidExitFullScreenNotification: a one-shot observer
    // signals it, and the windowing waits for it (at most 1.5 s after the flag clears).
    let (ended_tx, ended_rx) = mpsc::channel::<()>();
    exit_observer::watch(app, ended_tx);
    let handle=app.clone();
    let _=std::thread::Builder::new().name("pebble-stealth-windowed".into()).spawn(move || {
        let deadline=Instant::now()+Duration::from_secs(5);
        let began=Instant::now();
        loop {
            std::thread::sleep(Duration::from_millis(30));
            let (tx, rx) = mpsc::channel();
            let probe=handle.clone();
            let _=handle.run_on_main_thread(move || { let _=tx.send(main_fullscreen(&probe)); });
            if !rx.recv_timeout(Duration::from_secs(1)).unwrap_or(true) || Instant::now()>deadline { break; }
        }
        diag::log("full_screen_exit_seen", json!({"after_ms": began.elapsed().as_millis() as u64}));
        let ended=ended_rx.recv_timeout(Duration::from_millis(1500)).is_ok();
        diag::log("full_screen_exit_ended", json!({"after_ms": began.elapsed().as_millis() as u64, "notified": ended}));
        let shown=handle.clone();
        let _=handle.run_on_main_thread(move || {
            exit_observer::stop();
            if !stealth_on() { return; }
            let Some(main)=shown.get_webview_window("main") else { return };
            let (mut width, mut height)=(WINDOWED_SIZE.0, WINDOWED_SIZE.1);
            let mut oversized=false;
            if let Ok(Some(monitor))=main.current_monitor() {
                let screen=monitor.size().to_logical::<f64>(monitor.scale_factor());
                width=width.min(screen.width*0.8);
                height=height.min(screen.height*0.8);
                // AppKit restores the frame the workspace had before full screen (open_workspace sizes it first); only a
                // larger one is resized. Inner sizes: the outer one counts the title bar.
                if let Ok(now)=main.inner_size() {
                    let now=now.to_logical::<f64>(monitor.scale_factor());
                    oversized=now.width>width+2.0 || now.height>height+2.0;
                }
            }
            diag::log("windowing", json!({"oversized": oversized}));
            if oversized {
                let _=main.set_size(tauri::LogicalSize::new(width, height));
                let _=main.center();
            }
            if let Ok(ns)=main.ns_window() {
                let ns=ns as *mut objc2::runtime::AnyObject;
                let _=stealth_mac::set_panel(ns, true);
                stealth_mac::show_window_buttons(ns); // Phase 39: the first time, they stayed hidden
            }
            // Phase 39 (D83): out of the Dock and ⌘Tab now that the exit has ended (apply_stealth deferred it). The
            // companion is re-applied after the policy change once the desktop has slid in (D84), not mid-slide.
            yield_on_this_space(); // Phase 45 (F036)
            let _=shown.set_activation_policy(tauri::ActivationPolicy::Accessory);
            let later=shown.clone();
            let _=std::thread::Builder::new().name("pebble-companion-rejoin".into()).spawn(move || {
                std::thread::sleep(Duration::from_millis(400));
                let main_thread=later.clone();
                let _=later.run_on_main_thread(move || { companion_over_full_screen(&main_thread); companion_rejoin(&main_thread, "after_windowing"); });
            });
            diag::snapshot(&shown, "windowed");
            diag::snapshots_after(&shown, "windowed", &[100, 500, 1500]);
        });
    });
}
#[cfg(not(target_os="macos"))]
fn windowed_after_full_screen(_app:&tauri::AppHandle) {}
const WINDOWED_SIZE: (f64, f64) = (1160.0, 800.0); // the workspace's configured size (tauri.conf.json)

/// Whether a window is on the active Space (main thread); elsewhere than macOS, always.
fn on_active_space(window:&tauri::WebviewWindow) -> bool {
    #[cfg(target_os="macos")]
    {
        use objc2::{msg_send, runtime::{AnyObject, Bool}};
        let Ok(ns)=window.ns_window() else { return true };
        let here: Bool = unsafe { msg_send![ns as *mut AnyObject, isOnActiveSpace] };
        here.as_bool()
    }
    #[cfg(not(target_os="macos"))]
    { let _=window; true }
}

/// Phase 39 (D83): the workspace's windowed size (WINDOWED_SIZE within 80 % of its screen), set and centred only when
/// its inner size differs; returns whether it changed anything.
fn windowed_frame(main:&tauri::WebviewWindow) -> bool {
    let (mut width, mut height)=(WINDOWED_SIZE.0, WINDOWED_SIZE.1);
    let Ok(Some(monitor))=main.current_monitor() else { return false };
    let scale=monitor.scale_factor();
    let screen=monitor.size().to_logical::<f64>(scale);
    width=width.min(screen.width*0.8);
    height=height.min(screen.height*0.8);
    let Ok(now)=main.inner_size() else { return false };
    let now=now.to_logical::<f64>(scale);
    if (now.width-width).abs()<2.0 && (now.height-height).abs()<2.0 { return false; }
    let _=main.set_size(tauri::LogicalSize::new(width, height));
    let _=main.center();
    true
}

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

/// ⌃⌥⌘H: every Pebble window hidden at once. Phase 39 (the person's report): the same keys bring back what they hid,
/// so pressing them again returns the companion and, if it was open, the workspace. "Hide Pebble now" in the menu only
/// hides (hide_now); the menu-bar icon or ⌘⇧Space also bring Pebble back.
static PANIC_HIDDEN: Mutex<Vec<&'static str>> = Mutex::new(Vec::new()); // what the keys or the menu hid
fn stealth_panic(app:&tauri::AppHandle) {
    diag::log("stealth_panic", json!({"visible": visible_windows(app)}));
    if visible_windows(app).is_empty() {
        let hidden=std::mem::take(&mut *PANIC_HIDDEN.lock().unwrap());
        if hidden.contains(&"main") { reveal(app); }
        if hidden.is_empty() || hidden.contains(&"pet") {
            if let Some(w)=app.get_webview_window("pet") { let _=w.show(); }
            #[cfg(target_os="macos")]
            companion_over_full_screen(app);
        }
        return;
    }
    *PANIC_HIDDEN.lock().unwrap()=visible_windows(app);
    for label in ["pet", "main"] { if let Some(w)=app.get_webview_window(label) { let _=w.hide(); } }
}
/// "Hide Pebble now" (the menu): hide only, remembering what was shown for ⌃⌥⌘H.
fn hide_now(app:&tauri::AppHandle) {
    let shown=visible_windows(app);
    if !shown.is_empty() { *PANIC_HIDDEN.lock().unwrap()=shown; }
    for label in ["pet", "main"] { if let Some(w)=app.get_webview_window(label) { let _=w.hide(); } }
}
fn visible_windows(app:&tauri::AppHandle) -> Vec<&'static str> {
    ["pet", "main"].into_iter().filter(|label| app.get_webview_window(label).and_then(|w| w.is_visible().ok()).unwrap_or(false)).collect()
}

/// The menu-bar icon: a template glyph (an outline pebble; filled with a ring while on) and the item's check mark.
fn tray_state(app:&tauri::AppHandle, on:bool) {
    if let Some(tray)=app.tray_by_id("pebble") {
        let glyph=if on {include_bytes!("../icons/tray-on.png").as_slice()} else {include_bytes!("../icons/tray-off.png").as_slice()};
        if let Ok(image)=tauri::image::Image::from_bytes(glyph) { let _=tray.set_icon_with_as_template(Some(image), true); }
        #[cfg(target_os="macos")]
        tray_label(&tray, on);
    }
    if let Some(item)=app.state::<StealthItem>().0.lock().unwrap().as_ref() { let _=item.set_checked(on); }
}
struct StealthItem(Mutex<Option<tauri::menu::CheckMenuItem<tauri::Wry>>>);

/// Phase 45 (F035, audit round R1): the icon had no accessibility label (VoiceOver read nothing); Tauri sets only a
/// tooltip, so the status item's button gets one, with the Stealth state.
#[cfg(target_os="macos")]
fn tray_label(tray:&tauri::tray::TrayIcon, on:bool) {
    // Phase 46 (1.9.29, C7): the label also says when a phone is connected to Pebble Remote (remote_mark).
    let label=match (on, REMOTE_CONNECTED.load(Ordering::SeqCst)) {
        (true, true) => "Pebble, Stealth Lockdown on, an iPhone is connected",
        (true, false) => "Pebble, Stealth Lockdown on",
        (false, true) => "Pebble, an iPhone is connected",
        (false, false) => "Pebble",
    };
    let _=tray.with_inner_tray_icon(move |inner| {
        let Some(item)=inner.ns_status_item() else { return };
        let item=objc2::rc::Retained::as_ptr(&item) as *mut objc2::runtime::AnyObject;
        unsafe {
            let button: *mut objc2::runtime::AnyObject = objc2::msg_send![item, button];
            if !button.is_null() {
                let text=objc2_foundation::NSString::from_str(label);
                let _: () = objc2::msg_send![button, setAccessibilityLabel: &*text];
            }
        }
    });
}

#[tauri::command]
fn open_workspace(app:tauri::AppHandle, chat_id:Option<String>) -> Result<(),String> {
    diag::log("open_workspace", json!({"stealth": stealth_on(), "full": WORKSPACE_FULL.load(Ordering::SeqCst)}));
    diag::snapshots_after(&app, "open_workspace", &[300, 1000, 2000]);
    // Phase 39 (D82): in Stealth, from another Space (a full-screen app), go to the desktop first, then open there.
    #[cfg(target_os="macos")]
    if stealth_on() && space_anchor::elsewhere() && !space_anchor::GOING.swap(true, Ordering::SeqCst) {
        let handle=app.clone();
        space_anchor::go_then(&app, move || {
            let _=open_workspace(handle, chat_id);
            space_anchor::GOING.store(false, Ordering::SeqCst);
        });
        return Ok(());
    }
    #[cfg(target_os="macos")]
    let from_elsewhere=space_anchor::elsewhere(); // D85: another Space (a full-screen app) before Pebble activates
    #[cfg(not(target_os="macos"))]
    let from_elsewhere=false;
    reveal(&app);
    if let Some(main)=app.get_webview_window("main") {
        let _=main.set_always_on_top(false); // the floating companion size is always on top; full screen is not
        // Phase 33: under Stealth Lockdown the workspace opens windowed; its own full-screen Space would take over
        // the screen the person is presenting or sharing.
        // Phase 39 (D83): the frame macOS restores when Stealth takes the workspace out of full screen is the one it
        // had going in, so it gets its windowed size first (within 80 % of the screen, centred): the exit then lands
        // on it in one animation, with no second resize (diagnostic session 3).
        // D146: windowed when the person left it windowed (its frame kept as they left it)
        let full=WORKSPACE_FULL.load(Ordering::SeqCst);
        if full && !stealth_on() && !main.is_fullscreen().unwrap_or(false) { let _=windowed_frame(&main); }
        // Phase 39 (D85): from another app's full-screen Space, activating Pebble first slides to the desktop, and
        // full screen requested during that slide was undone (diagnostic sessions 5-6, E4); it waits until the
        // workspace is on the active Space (at most a second), as the Stealth exit waits for its end (D83).
        let enter=move |main:&tauri::WebviewWindow| {
            if full && !stealth_on() && !main.is_fullscreen().unwrap_or(false) { let _=main.set_fullscreen(true); }
        };
        if !from_elsewhere || on_active_space(&main) { enter(&main); }
        else {
            let handle=app.clone();
            let _=std::thread::Builder::new().name("pebble-workspace-space".into()).spawn(move || {
                let began=Instant::now();
                while began.elapsed()<Duration::from_millis(1000) {
                    std::thread::sleep(Duration::from_millis(30));
                    let (tx, rx)=mpsc::channel();
                    let probe=handle.clone();
                    let _=handle.run_on_main_thread(move || {
                        let _=tx.send(probe.get_webview_window("main").map(|m| on_active_space(&m)).unwrap_or(true));
                    });
                    if rx.recv_timeout(Duration::from_secs(1)).unwrap_or(true) { break; }
                }
                diag::log("workspace_on_active_space", json!({"after_ms": began.elapsed().as_millis() as u64}));
                let shown=handle.clone();
                let _=handle.run_on_main_thread(move || { if let Some(main)=shown.get_webview_window("main") { enter(&main); } });
            });
        }
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
    // Phase 45 (F164, audit round R5): layer 4's two switches (PEBBLE_SCREEN_CAPTURE, PEBBLE_FOREGROUND_INPUT) are
    // Pebble's own and hold no secret; left out, a launch with them never turned the lanes on.
    #[cfg(unix)] {
        command.env_clear();
        for key in ["PATH","HOME","USER","LOGNAME","SHELL","LANG","LC_ALL","LC_CTYPE","TMPDIR","__CF_USER_TEXT_ENCODING","PEBBLE_SCREEN_CAPTURE","PEBBLE_FOREGROUND_INPUT"] {
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
                    #[cfg(target_os="macos")]
                    if value.get("event").and_then(Value::as_str)==Some("reply_banner") { reply_banner(&handle, value.get("data").cloned().unwrap_or(Value::Null)); }
                    // Phase 46 (1.9.29, D214, C7): the Mac shows while a phone is connected to Pebble Remote, and what it did.
                    if value.get("event").and_then(Value::as_str)==Some("remote_state") { remote_mark(&handle, value.pointer("/data/connected").and_then(Value::as_u64).unwrap_or(0)>0); }
                    #[cfg(target_os="macos")]
                    if value.get("event").and_then(Value::as_str)==Some("remote_notice") { remote_notice(&handle, value.get("data").cloned().unwrap_or(Value::Null)); }
                    let _=handle.emit("backend-event",&value);
                }
            }
        }
        for (_,sender) in pending.lock().unwrap().drain() { let _=sender.send(Err("Local service stopped.".into())); }
        let _=handle.emit("backend-event",json!({"event":"offline","data":{}}));
    });
    Ok(())
}

/// Phase 45 (D134, the person, 2026-10-26): the backend's banner for a task that ended or waits (notifications and replies
/// on, outside quiet hours, not a scheduled run) is shown only while Pebble is not the active app and Stealth Lockdown is
/// off, through the Open-only banner code (Open opens the conversation; there is no Approve). The post waits on macOS
/// off the main thread.
#[cfg(target_os="macos")]
fn reply_banner(app:&tauri::AppHandle, data:Value) {
    let _=app.run_on_main_thread(move || {
        if app_active() || stealth_on() { return; }
        let Some(run)=data.get("run_id").and_then(Value::as_str).filter(|value| hex32(value)).map(str::to_owned) else { return };
        let _=std::thread::Builder::new().name("pebble-reply-banner".into()).spawn(move || {
            let Some(center)=un::center() else { return };
            let text=|key:&str, fallback:&str| data.get(key).and_then(Value::as_str).unwrap_or(fallback).chars().take(140).collect::<String>();
            let post=json!({"title": text("title", "Pebble"), "body": text("body", ""), "thread": "pebble.replies",
                            "chat_id": text("chat_id", "")});
            let outcome=un::post(&center, &post, &format!("reply-{run}"), 1);
            diag::log("reply_banner", json!({"posted": matches!(outcome, Ok(true)), "error": outcome.err()}));
        });
    });
}

/// Phase 46 (1.9.29, D214, C7): what a phone did that the person should see on the Mac (an approval, something opened on
/// this Mac, a screenshot, a new pairing) as a banner, shown while Pebble is not the active app and Stealth Lockdown is
/// off, as reply banners are. Open only: the banner carries no action.
#[cfg(target_os="macos")]
fn remote_notice(app:&tauri::AppHandle, data:Value) {
    if data.get("banner").and_then(Value::as_bool)!=Some(true) { return; }
    let Some(id)=data.get("id").and_then(Value::as_str).filter(|value| hex32(value)).map(str::to_owned) else { return };
    let _=app.run_on_main_thread(move || {
        if app_active() || stealth_on() { return; }
        let _=std::thread::Builder::new().name("pebble-remote-notice".into()).spawn(move || {
            let Some(center)=un::center() else { return };
            let body=data.get("text").and_then(Value::as_str).unwrap_or("").chars().take(160).collect::<String>();
            let post=json!({"title": "Pebble Remote", "body": body, "thread": "pebble.remote"});
            let outcome=un::post(&center, &post, &format!("remote-{id}"), 1);
            diag::log("remote_notice", json!({"posted": matches!(outcome, Ok(true)), "error": outcome.err()}));
        });
    });
}

/// Phase 46 (1.9.29, D214, C7): while a phone is connected to Pebble Remote the menu-bar icon carries a mark (a dot beside
/// the glyph), and its tooltip and accessibility label say so. The backend's remote_state event carries the count.
static REMOTE_CONNECTED: AtomicBool = AtomicBool::new(false);
fn remote_mark(app:&tauri::AppHandle, connected:bool) {
    if REMOTE_CONNECTED.swap(connected, Ordering::SeqCst)==connected { return; }
    if let Some(tray)=app.tray_by_id("pebble") {
        let _=tray.set_title(if connected {Some("•")} else {None::<&str>});
        let _=tray.set_tooltip(Some(if connected {"Pebble · an iPhone is connected"} else {"Pebble"}));
        #[cfg(target_os="macos")]
        tray_label(&tray, stealth_on());
    }
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
static PET_HOME: std::sync::Mutex<Option<(i32,i32)>> = std::sync::Mutex::new(None); // Phase 45 (F061)
static PANEL_HOME: std::sync::Mutex<Option<(i32,i32)>> = std::sync::Mutex::new(None); // Phase 45 (F065)

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
    // Phase 45 (F061): parked in the top half, the pebble opens its chat downwards (top edge kept), and when the chat
    // closes it returns to the place it was parked, wherever opening had to move it.
    let collapsed=(old.width as f64/scale-112.0).abs()<1.0 && (old.height as f64/scale-112.0).abs()<1.0;
    let opening=collapsed && (width>112.0 || height>112.0);
    if opening { *PET_HOME.lock().unwrap()=Some((pos.x, pos.y)); }
    // Phase 45 (F065, audit round R2): near the left edge the sidebar's growth to the left was clamped, so closing it
    // moved the panel (and the pebble) 360 points to the right. The panel's place before the sidebar opened is where
    // it goes back. (The sidebar layout is 744 points wide, the panel alone 384.)
    let sidebar_was=old.width as f64/scale>500.0;
    let sidebar_now=expanded && sidebar.unwrap_or(false);
    if sidebar_now && !sidebar_was && !collapsed { *PANEL_HOME.lock().unwrap()=Some((pos.x, pos.y)); }
    let upper=monitor.as_ref().map(|m| (pos.y as f64+old.height as f64/2.0) < (m.position().y as f64+m.size().height as f64/2.0)).unwrap_or(false);
    let mut x=pos.x as f64+(old.width as f64-width*scale);
    let mut y=if upper && opening { pos.y as f64 } else { pos.y as f64+(old.height as f64-height*scale) };
    if !expanded && !status.unwrap_or(false) {
        if let Some((hx, hy))=PET_HOME.lock().unwrap().take() { x=hx as f64; y=hy as f64; }
    }
    if sidebar_was && !sidebar_now {
        let home=PANEL_HOME.lock().unwrap().take();
        if let (true, Some((px, py)))=(expanded, home) { x=px as f64; y=py as f64; }
    }
    if let Some(m)=monitor {
        x=x.max(m.position().x as f64).min(m.position().x as f64+m.size().width as f64-width*scale);
        y=y.max(m.position().y as f64+24.0*scale).min(m.position().y as f64+m.size().height as f64-height*scale);
    }
    window.set_size(tauri::LogicalSize::new(width,height)).map_err(|e|e.to_string())?;
    window.set_position(tauri::PhysicalPosition::new(x as i32,y as i32)).map_err(|e|e.to_string())?;
    pet_passthrough(&app, !expanded); // Phase 45 (F085): open to chat, it always takes clicks
    if expanded { focus(&window); }
    Ok(())
}

// Phase 45 (F062, audit round R2): the pebble dragged over the Dock landed partly below the screen, behind the Dock,
// where it was hard to see or grab. Once it has stopped moving (0.35 s after its last move: a drag, or a resize by
// pet_expand) it is put back inside the screen's work area, the menu bar and the Dock left out. One waiting thread at a
// time.
static PET_MOVED_AT: AtomicU64 = AtomicU64::new(0);
static PET_SETTLING: AtomicBool = AtomicBool::new(false);

fn epoch_ms() -> u64 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_millis() as u64).unwrap_or(0)
}

fn pet_moved(app:&tauri::AppHandle) {
    PET_MOVED_AT.store(epoch_ms(), Ordering::SeqCst);
    if PET_SETTLING.swap(true, Ordering::SeqCst) { return; }
    let app=app.clone();
    let spawned=std::thread::Builder::new().name("pebble-pet-settle".into()).spawn(move || {
        loop {
            std::thread::sleep(Duration::from_millis(120));
            if epoch_ms().saturating_sub(PET_MOVED_AT.load(Ordering::SeqCst)) >= 350 { break; }
        }
        PET_SETTLING.store(false, Ordering::SeqCst);
        let main_thread=app.clone();
        let _=app.run_on_main_thread(move || settle_pet(&main_thread));
    });
    if spawned.is_err() { PET_SETTLING.store(false, Ordering::SeqCst); }
}

fn settle_pet(app:&tauri::AppHandle) {
    let Some(pet)=app.get_webview_window("pet") else { return };
    let (Ok(pos), Ok(size), Ok(Some(monitor)))=(pet.outer_position(), pet.outer_size(), pet.current_monitor()) else { return };
    let area=monitor.work_area();
    let (ax, ay, aw, ah)=(area.position.x, area.position.y, area.size.width as i32, area.size.height as i32);
    let x=pos.x.min(ax+aw-size.width as i32).max(ax);
    let y=pos.y.min(ay+ah-size.height as i32).max(ay);
    if (x, y)!=(pos.x, pos.y) {
        let _=pet.set_position(tauri::PhysicalPosition::new(x, y));
        diag::log("pet_settled", json!({"from": [pos.x, pos.y], "to": [x, y]}));
    }
}

// Phase 45 (F085, audit round R3): over the workspace in front (in full screen nothing is beside it), the pebble's window
// covered the review card's Allow button: the click opened the Companion instead of allowing, and its status pill hid the
// card's last words. While the workspace is the key window and the pebble overlaps it (closed, not chatting), clicks go
// through the pebble to the workspace and the pebble is not drawn (R4: dimmed, its pill still covered the card); both
// come back when the workspace hides or another window or app is in front.
fn pet_passthrough(app:&tauri::AppHandle, wanted:bool) {
    let Some(pet)=app.get_webview_window("pet") else { return };
    let closed=pet.inner_size().ok().zip(pet.scale_factor().ok()).is_some_and(|(size, scale)| size.width as f64/scale<200.0);
    let on=wanted && closed && app.get_webview_window("main").is_some_and(|main|
        main.is_visible().unwrap_or(false) && main.is_focused().unwrap_or(false) && overlaps(&main, &pet));
    let _=pet.set_ignore_cursor_events(on);
    #[cfg(target_os="macos")]
    if let Ok(ns)=pet.ns_window() {
        // F122 (audit round R4): dimmed to 0.4, its "Waiting for your review" pill still lay across the card's words;
        // over the focused workspace (which shows the same status) the pebble is not drawn at all
        unsafe { let _: () = objc2::msg_send![ns as *mut objc2::runtime::AnyObject, setAlphaValue: if on {0.0f64} else {1.0f64}]; }
    }
    diag::log("pet_passthrough", json!({"on": on}));
}

fn overlaps(a:&tauri::WebviewWindow, b:&tauri::WebviewWindow) -> bool {
    let (Ok(ap), Ok(asz), Ok(bp), Ok(bsz))=(a.outer_position(), a.outer_size(), b.outer_position(), b.outer_size()) else { return false };
    ap.x < bp.x+bsz.width as i32 && bp.x < ap.x+asz.width as i32 && ap.y < bp.y+bsz.height as i32 && bp.y < ap.y+asz.height as i32
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
        companion_over_full_screen(app); // Phase 38: also over other apps' full-screen Spaces
    }
    Ok(())
}

/// Phase 39 (D82, the person's report on the 1.5.2 test build): in Stealth Lockdown, "Open full workspace" from the
/// companion over another app's full screen opened the workspace window over that full-screen app. Measured on macOS 27
/// (`.runtime/phase39-fsprobe`, probes G-I): an accessory app's windows open in whatever Space is active, full-screen
/// ones included, whatever their class or Space behaviour; activating the app switches to the desktop only when one of
/// its windows already lives there and is made key, and the companion (on every Space) does not count. So a 1x1
/// transparent window that ignores the mouse and is hidden from capture lives on the desktop Space from launch; in
/// Stealth, when it is not on the active Space, opening the workspace activates Pebble through it, waits for the switch
/// (at most a second), then opens the workspace there, as an app opens on the desktop.
#[cfg(target_os="macos")]
mod space_anchor {
    use objc2::{class, ffi, msg_send, runtime::{AnyClass, AnyObject, Bool, Imp, Sel}, sel, encode::{Encode, Encoding}};
    use std::sync::{atomic::{AtomicBool, AtomicUsize, Ordering}, mpsc};
    use std::time::{Duration, Instant};

    static ANCHOR: AtomicUsize = AtomicUsize::new(0);
    pub static GOING: AtomicBool = AtomicBool::new(false); // an open in progress: the second call opens directly

    #[repr(C)]
    #[derive(Clone, Copy)]
    struct Rect { x: f64, y: f64, width: f64, height: f64 }
    unsafe impl Encode for Rect {
        const ENCODING: Encoding = Encoding::Struct("CGRect", &[
            Encoding::Struct("CGPoint", &[Encoding::Double, Encoding::Double]),
            Encoding::Struct("CGSize", &[Encoding::Double, Encoding::Double])]);
    }

    extern "C-unwind" fn yes(_this: *mut AnyObject, _sel: Sel) -> Bool { Bool::YES }

    /// An NSWindow subclass that may become key (a borderless window may not), made once.
    fn class() -> Option<&'static AnyClass> {
        static CLASS: std::sync::OnceLock<Option<usize>> = std::sync::OnceLock::new();
        let made = *CLASS.get_or_init(|| unsafe {
            let window = AnyClass::get(c"NSWindow")?;
            let cls = ffi::objc_allocateClassPair(window, c"PebbleSpaceAnchor".as_ptr(), 0);
            if cls.is_null() { return None; }
            let method = ffi::class_getInstanceMethod(window, sel!(canBecomeKeyWindow));
            let imp: Imp = std::mem::transmute(yes as extern "C-unwind" fn(*mut AnyObject, Sel) -> Bool);
            if !ffi::class_addMethod(cls, sel!(canBecomeKeyWindow), imp, ffi::method_getTypeEncoding(method)).as_bool() { return None; }
            ffi::objc_registerClassPair(cls);
            Some(cls as usize)
        });
        made.map(|ptr| unsafe { &*(ptr as *const AnyClass) })
    }

    #[link(name = "ApplicationServices", kind = "framework")]
    extern "C" {
        fn AXIsProcessTrusted() -> u8;
        fn AXUIElementCreateApplication(pid: i32) -> *const std::ffi::c_void;
        fn AXUIElementCopyAttributeValue(element: *const std::ffi::c_void, attribute: *const std::ffi::c_void,
            value: *mut *const std::ffi::c_void) -> i32;
        fn AXUIElementSetMessagingTimeout(element: *const std::ffi::c_void, seconds: f32) -> i32;
    }
    #[link(name = "CoreFoundation", kind = "framework")]
    extern "C" {
        fn CFRelease(cf: *const std::ffi::c_void);
        fn CFGetTypeID(cf: *const std::ffi::c_void) -> usize;
        fn CFBooleanGetTypeID() -> usize;
        fn CFBooleanGetValue(boolean: *const std::ffi::c_void) -> u8;
    }

    static CHECKING: AtomicBool = AtomicBool::new(false);

    /// The app that owns the menu bar (the full-screen app, on its Space; never an accessory app such as Pebble). Main
    /// thread.
    fn menu_bar_owner() -> i32 {
        use objc2::rc::Retained;
        let Some(class) = AnyClass::get(c"NSWorkspace") else { return 0 };
        unsafe {
            let workspace: Option<Retained<AnyObject>> = msg_send![class, sharedWorkspace];
            let Some(workspace) = workspace else { return 0 };
            let owner: Option<Retained<AnyObject>> = msg_send![&*workspace, menuBarOwningApplication];
            owner.map(|owner| { let pid: i32 = msg_send![&*owner, processIdentifier]; pid }).unwrap_or(0)
        }
    }

    /// Phase 42: whether the active Space is another app's full-screen Space: the AXFullScreen of the menu bar app's
    /// focused (else main) window. None without Pebble's Accessibility permission, for Pebble itself, or when the app
    /// does not answer within 0.25 s. Any thread. (The wallpaper cannot tell: macOS 27 lists it on full-screen Spaces
    /// too, measured 2026-10-25.)
    fn full_screen_space(pid: i32) -> Option<bool> {
        use objc2::rc::Retained;
        use objc2_foundation::NSString;
        if pid <= 0 || pid as u32 == std::process::id() { return None; }
        unsafe {
            if AXIsProcessTrusted() == 0 { return None; }
            let app = AXUIElementCreateApplication(pid);
            if app.is_null() { return None; }
            AXUIElementSetMessagingTimeout(app, 0.25);
            let full = NSString::from_str("AXFullScreen");
            let mut answer = Some(false); // no window at all: nothing of the menu bar's app is full screen here
            for name in ["AXFocusedWindow", "AXMainWindow"] {
                let attribute = NSString::from_str(name);
                let mut window: *const std::ffi::c_void = std::ptr::null();
                let code = AXUIElementCopyAttributeValue(app, Retained::as_ptr(&attribute) as *const std::ffi::c_void, &mut window);
                if code == -25204 || code == -25211 { answer = None; break; } // did not answer; no permission after all
                if code != 0 || window.is_null() { continue; }
                let mut value: *const std::ffi::c_void = std::ptr::null();
                let read = AXUIElementCopyAttributeValue(window, Retained::as_ptr(&full) as *const std::ffi::c_void, &mut value);
                answer = if read == -25204 { None } else {
                    Some(read == 0 && !value.is_null() && CFGetTypeID(value) == CFBooleanGetTypeID() && CFBooleanGetValue(value) != 0)
                };
                if !value.is_null() { CFRelease(value); }
                CFRelease(window);
                break;
            }
            CFRelease(app);
            answer
        }
    }

    /// Phase 42: the anchor is made on a desktop Space only. Measured 2026-10-25: launched while a full-screen app was
    /// in front, the anchor stayed on that app's Space and Stealth's workspace, opened from the desktop, went there.
    /// Reads the menu bar's app here (the main thread), checks it off the main thread, then makes the anchor on the
    /// main thread, or leaves it for the next Space change when the active Space is another app's full-screen Space.
    /// Unknown (no Accessibility permission) makes it at once, as before. A no-op once the anchor exists.
    pub fn place(app: &tauri::AppHandle, why: &'static str) {
        if window().is_some() || CHECKING.swap(true, Ordering::SeqCst) { return; }
        let pid = menu_bar_owner();
        let handle = app.clone();
        let spawned = std::thread::Builder::new().name("pebble-space-anchor-place".into()).spawn(move || {
            let full = full_screen_space(pid);
            let _ = handle.run_on_main_thread(move || {
                CHECKING.store(false, Ordering::SeqCst);
                if window().is_some() { return; }
                if full != Some(true) { create(); }
                crate::diag::log("space_anchor", serde_json::json!({"why": why, "full_screen_space": full, "made": window().is_some()}));
            });
        });
        if spawned.is_err() { CHECKING.store(false, Ordering::SeqCst); create(); }
    }

    /// The anchor, ordered in on the active Space (through place, which checks that Space first). Main thread.
    fn create() {
        let Some(cls) = class() else { return };
        unsafe {
            let window: *mut AnyObject = msg_send![cls, alloc];
            let rect = Rect { x: 0.0, y: 0.0, width: 1.0, height: 1.0 };
            let window: *mut AnyObject = msg_send![window, initWithContentRect: rect, styleMask: 0usize, backing: 2usize, defer: Bool::NO];
            if window.is_null() { return; }
            let clear: *mut AnyObject = msg_send![class!(NSColor), clearColor];
            let _: () = msg_send![window, setOpaque: Bool::NO];
            let _: () = msg_send![window, setBackgroundColor: clear];
            let _: () = msg_send![window, setIgnoresMouseEvents: Bool::YES];
            let _: () = msg_send![window, setSharingType: 0usize];       // NSWindowSharingNone
            let _: () = msg_send![window, setHasShadow: Bool::NO];
            let _: () = msg_send![window, setReleasedWhenClosed: Bool::NO];
            let _: () = msg_send![window, setCollectionBehavior: (1usize << 2) | (1usize << 6)]; // Managed | IgnoresCycle
            let _: () = msg_send![window, orderFrontRegardless];
            ANCHOR.store(window as usize, Ordering::SeqCst);
        }
    }

    fn window() -> Option<*mut AnyObject> {
        let ptr = ANCHOR.load(Ordering::SeqCst);
        (ptr != 0).then_some(ptr as *mut AnyObject)
    }

    /// Whether the person is in another Space than the anchor's (a full-screen app, usually). Main thread.
    pub fn elsewhere() -> bool {
        let Some(window) = window() else { return false };
        let here: Bool = unsafe { msg_send![window, isOnActiveSpace] };
        !here.as_bool()
    }

    /// Activate Pebble through the anchor (macOS switches to its Space), wait for the switch, then run `then` on the
    /// main thread. Main thread.
    pub fn go_then(app: &tauri::AppHandle, then: impl FnOnce() + Send + 'static) {
        if let Some(window) = window() {
            unsafe {
                let ns_app: *mut AnyObject = msg_send![class!(NSApplication), sharedApplication];
                let _: () = msg_send![ns_app, activateIgnoringOtherApps: Bool::YES];
                let _: () = msg_send![window, makeKeyAndOrderFront: std::ptr::null::<AnyObject>()];
            }
        }
        let handle = app.clone();
        let _ = std::thread::Builder::new().name("pebble-space-anchor".into()).spawn(move || {
            let began = Instant::now();
            while began.elapsed() < Duration::from_millis(1000) {
                std::thread::sleep(Duration::from_millis(30));
                let (tx, rx) = mpsc::channel();
                let _ = handle.run_on_main_thread(move || { let _ = tx.send(!elsewhere()); });
                if rx.recv_timeout(Duration::from_secs(1)).unwrap_or(false) { break; }
            }
            crate::diag::log("space_switch", serde_json::json!({"after_ms": began.elapsed().as_millis() as u64}));
            let _ = handle.run_on_main_thread(then);
        });
    }
}

/// Phase 39 (D84): a visible companion left out of the active Space is ordered in there (orderFrontRegardless: not
/// made key, Pebble not activated) and its Space behaviour re-applied; re-applying alone did not bring it back
/// (diagnostic session 5). Main thread.
#[cfg(target_os="macos")]
fn companion_rejoin(app:&tauri::AppHandle, why:&str) {
    use objc2::{msg_send, runtime::{AnyObject, Bool}};
    let Some(pet)=app.get_webview_window("pet") else { return };
    if !pet.is_visible().unwrap_or(false) { return; }
    // Only in companion mode (an accessory app): while the workspace is open Pebble is an ordinary app, whose windows
    // macOS keeps out of other apps' full-screen Spaces, and ordering one in there could pull the person to another
    // Space.
    let policy: isize = unsafe {
        let ns_app: *mut AnyObject = msg_send![objc2::class!(NSApplication), sharedApplication];
        msg_send![ns_app, activationPolicy]
    };
    if policy != 1 { return; }
    let Ok(ns)=pet.ns_window() else { return };
    let ns=ns as *mut AnyObject;
    let here: Bool = unsafe { msg_send![ns, isOnActiveSpace] };
    if here.as_bool() { return; }
    unsafe { let _: () = msg_send![ns, orderFrontRegardless]; }
    companion_over_full_screen(app);
    let now: Bool = unsafe { msg_send![ns, isOnActiveSpace] };
    diag::log("companion_rejoined", json!({"why": why, "on_active_space_now": now.as_bool()}));
}

/// Phase 40 (1.6.0): right after launch the companion could be visible yet not on the Space Pebble started on, until the
/// first Space change brought it in (session 7's launch snapshot; a read-only window check after the 1.5.3 install). The
/// rejoin a Space change runs is run three times after launch (0.3, 1 and 2.5 s), on the main thread; it changes
/// nothing when the companion is already on the active Space, and nothing at all outside companion mode.
#[cfg(target_os="macos")]
fn companion_rejoin_after_launch(app:&tauri::AppHandle) {
    let handle=app.clone();
    std::thread::spawn(move || {
        for wait in [300u64, 700, 1500] {
            std::thread::sleep(std::time::Duration::from_millis(wait));
            let main_thread=handle.clone();
            let _=handle.run_on_main_thread(move || companion_rejoin(&main_thread, "launch"));
        }
    });
}

/// Phase 39 (D84, diagnostic sessions 2-4): re-applying the companion's Space behaviour while macOS is still sliding to
/// another Space (after the workspace leaves full screen, for one) left the companion out of the new Space until the
/// next mode switch. After every Space change (NSWorkspaceActiveSpaceDidChangeNotification, delivered once the switch
/// has ended), a visible companion that is not on the active Space has its behaviour re-applied, which brings it back.
#[cfg(target_os="macos")]
fn companion_follows_spaces(app:&tauri::AppHandle) {
    use block2::RcBlock;
    use objc2::{msg_send, rc::Retained, runtime::{AnyClass, AnyObject}};
    use objc2_foundation::NSString;
    let Some(workspace_class)=AnyClass::get(c"NSWorkspace") else { return };
    let handle=app.clone();
    unsafe {
        let workspace: Option<Retained<AnyObject>> = msg_send![workspace_class, sharedWorkspace];
        let center: Option<Retained<AnyObject>> = workspace.and_then(|w| msg_send![&*w, notificationCenter]);
        let Some(center)=center else { return };
        // Phase 42: a Space change also makes the anchor, if launch left it for a desktop Space.
        let block=RcBlock::new(move |_note: std::ptr::NonNull<AnyObject>| { companion_rejoin(&handle, "space_changed"); space_anchor::place(&handle, "space_changed"); });
        let token: Option<Retained<AnyObject>> = msg_send![&*center, addObserverForName: &*NSString::from_str("NSWorkspaceActiveSpaceDidChangeNotification"),
            object: Option::<&AnyObject>::None, queue: Option::<&AnyObject>::None, usingBlock: &*block];
        std::mem::forget(token); // for the app's lifetime
    }
}

/// Phase 39 (D81): a developer diagnostic for the shell's native window behaviour, off unless Pebble is launched with
/// PEBBLE_SHELL_DIAG naming an existing folder owned by the person. Then the shell appends timed JSON lines to
/// `<folder>/shell.jsonl` (window class, style, Spaces, visibility, the title bar buttons, the app's policy, main-thread
/// stalls) and runs one fixed command at a time from `<folder>/command` through the same functions the UI uses:
/// `stealth on|off`, `open_workspace`, `to_pet`, `panic`, `state`, `latency <ms>`, and (Phase 42, E-N2)
/// `ax_probe <title fragment>`. Nothing else is read or run.
mod diag {
    use serde_json::{json, Value};
    use std::{io::Write, path::PathBuf, sync::{mpsc, OnceLock}, time::{Duration, Instant}};

    static DIR: OnceLock<Option<PathBuf>> = OnceLock::new();
    static START: OnceLock<Instant> = OnceLock::new();

    pub fn dir() -> Option<&'static PathBuf> {
        DIR.get_or_init(|| {
            let dir = PathBuf::from(std::env::var_os("PEBBLE_SHELL_DIAG")?);
            #[cfg(unix)]
            {
                use std::os::unix::fs::MetadataExt;
                let meta = std::fs::metadata(&dir).ok()?;
                // the person's own folder, not writable by others
                if !meta.is_dir() || meta.uid() != unsafe { libc_getuid() } || meta.mode() & 0o022 != 0 { return None; }
            }
            Some(dir)
        }).as_ref()
    }
    extern "C" { #[link_name = "getuid"] fn libc_getuid() -> u32; }

    pub fn on() -> bool { dir().is_some() }

    pub fn log(event: &str, data: Value) {
        let Some(dir) = dir() else { return };
        let ms = START.get_or_init(Instant::now).elapsed().as_secs_f64() * 1000.0;
        let line = json!({"ms": (ms * 10.0).round() / 10.0, "event": event, "data": data}).to_string() + "\n";
        if let Ok(mut file) = std::fs::OpenOptions::new().create(true).append(true).open(dir.join("shell.jsonl")) {
            let _ = file.write_all(line.as_bytes()); // one write, so lines from different threads never interleave
        }
    }

    /// Main-thread round trips every 16 ms for `ms`; a stuck UI shows as long round trips.
    pub fn latency(app: &tauri::AppHandle, ms: u64, why: &'static str) {
        if !on() { return; }
        let handle = app.clone();
        let _ = std::thread::Builder::new().name("pebble-diag-latency".into()).spawn(move || {
            let end = Instant::now() + Duration::from_millis(ms);
            let mut samples: Vec<f64> = Vec::new();
            while Instant::now() < end {
                let (tx, rx) = mpsc::channel();
                let sent = Instant::now();
                let _ = handle.run_on_main_thread(move || { let _ = tx.send(()); });
                let waited = match rx.recv_timeout(Duration::from_secs(3)) { Ok(()) => sent.elapsed().as_secs_f64() * 1000.0, Err(_) => 3000.0 };
                samples.push(waited);
                std::thread::sleep(Duration::from_millis(16));
            }
            let mut sorted = samples.clone();
            sorted.sort_by(|a, b| a.partial_cmp(b).unwrap());
            let pick = |q: f64| sorted.get(((sorted.len() as f64 - 1.0) * q).round() as usize).copied().unwrap_or(0.0);
            log("main_thread_latency", json!({"why": why, "samples": samples.len(), "p50_ms": pick(0.5), "p95_ms": pick(0.95),
                "max_ms": pick(1.0), "over_100ms": samples.iter().filter(|s| **s > 100.0).count(),
                "over_250ms": samples.iter().filter(|s| **s > 250.0).count()}));
        });
    }

    /// The state of both windows and the app, logged from the main thread.
    #[cfg(target_os = "macos")]
    pub fn snapshot(app: &tauri::AppHandle, why: &str) {
        use objc2::{msg_send, runtime::{AnyObject, Bool}};
        use tauri::Manager;
        if !on() { return; }
        let mut windows = serde_json::Map::new();
        for label in ["pet", "main"] {
            let Some(window) = app.get_webview_window(label) else { continue };
            let Ok(ns) = window.ns_window() else { continue };
            let ns = ns as *mut AnyObject;
            let entry = unsafe {
                let isa = objc2::ffi::object_getClass(ns as *const AnyObject);
                let isa_name = if isa.is_null() { String::new() } else { std::ffi::CStr::from_ptr(objc2::ffi::class_getName(isa)).to_string_lossy().into_owned() };
                let class: *mut AnyObject = msg_send![ns, className];
                let class_name: *const std::ffi::c_char = if class.is_null() { std::ptr::null() } else { msg_send![class, UTF8String] };
                let class_name = if class_name.is_null() { String::new() } else { std::ffi::CStr::from_ptr(class_name).to_string_lossy().into_owned() };
                let mut buttons = serde_json::Map::new();
                for (name, kind) in [("close", 0usize), ("minimize", 1), ("zoom", 2)] {
                    let button: *mut AnyObject = msg_send![ns, standardWindowButton: kind];
                    if button.is_null() { buttons.insert(name.into(), Value::Null); continue; }
                    let hidden: Bool = msg_send![button, isHiddenOrHasHiddenAncestor];
                    let mut alphas = Vec::new();
                    let mut view = button;
                    for _ in 0..8 {
                        if view.is_null() { break; }
                        let alpha: f64 = msg_send![view, alphaValue];
                        alphas.push((alpha * 100.0).round() / 100.0);
                        view = msg_send![view, superview];
                    }
                    let in_window: *mut AnyObject = msg_send![button, window];
                    buttons.insert(name.into(), json!({"hidden_or_ancestor": hidden.as_bool(), "alphas_up": alphas,
                        "in_this_window": in_window == ns}));
                }
                let mask: usize = msg_send![ns, styleMask];
                let behaviour: usize = msg_send![ns, collectionBehavior];
                let level: isize = msg_send![ns, level];
                let sharing: usize = msg_send![ns, sharingType];
                let visible: Bool = msg_send![ns, isVisible];
                let key: Bool = msg_send![ns, isKeyWindow];
                let active_space: Bool = msg_send![ns, isOnActiveSpace];
                let alpha: f64 = msg_send![ns, alphaValue];
                let number: isize = msg_send![ns, windowNumber];
                json!({"class": class_name, "isa": isa_name, "style_mask": mask, "full_screen": mask & (1 << 14) != 0,
                       "nonactivating": mask & (1 << 7) != 0, "behaviour": behaviour, "level": level, "sharing": sharing,
                       "visible": visible.as_bool(), "key": key.as_bool(), "on_active_space": active_space.as_bool(),
                       "alpha": alpha, "number": number, "buttons": buttons})
            };
            let size = window.outer_size().ok().map(|s| json!([s.width, s.height]));
            let position = window.outer_position().ok().map(|p| json!([p.x, p.y]));
            let mut entry = entry;
            entry["outer_size"] = size.unwrap_or(Value::Null);
            entry["outer_position"] = position.unwrap_or(Value::Null);
            windows.insert(label.into(), entry);
        }
        let (policy, active) = unsafe {
            let ns_app: *mut AnyObject = msg_send![objc2::class!(NSApplication), sharedApplication];
            let policy: isize = msg_send![ns_app, activationPolicy];
            let active: Bool = msg_send![ns_app, isActive];
            (policy, active.as_bool())
        };
        log("snapshot", json!({"why": why, "policy": policy, "app_active": active, "windows": windows}));
    }
    #[cfg(not(target_os = "macos"))]
    pub fn snapshot(_app: &tauri::AppHandle, _why: &str) {}

    /// Snapshots after `delays` ms, each on the main thread.
    pub fn snapshots_after(app: &tauri::AppHandle, why: &'static str, delays: &'static [u64]) {
        if !on() { return; }
        for &delay in delays {
            let handle = app.clone();
            let _ = std::thread::Builder::new().name("pebble-diag-snapshot".into()).spawn(move || {
                std::thread::sleep(Duration::from_millis(delay));
                let shown = handle.clone();
                let _ = handle.run_on_main_thread(move || snapshot(&shown, &format!("{why} +{delay}ms")));
            });
        }
    }

    /// The command file, polled every 50 ms; each command runs on the main thread as the UI's own path would.
    pub fn commands(app: &tauri::AppHandle, run: fn(&tauri::AppHandle, &str)) {
        let Some(dir) = dir() else { return };
        let path = dir.join("command");
        let handle = app.clone();
        let _ = std::thread::Builder::new().name("pebble-diag-commands".into()).spawn(move || loop {
            std::thread::sleep(Duration::from_millis(50));
            let Ok(text) = std::fs::read_to_string(&path) else { continue };
            let _ = std::fs::remove_file(&path);
            for command in text.lines().map(str::trim).filter(|c| !c.is_empty()) {
                log("command", json!({"command": command}));
                run(&handle, command);
            }
        });
    }
}

/// The diagnostic's fixed commands (D81), each through the function the UI uses.
fn diag_run(app:&tauri::AppHandle, command:&str) {
    let mut words=command.split_whitespace();
    match (words.next(), words.next()) {
        (Some("stealth"), Some(state @ ("on"|"off"))) => {
            let next=state=="on";
            let backend=Arc::clone(&*app.state::<Arc<Backend>>());
            if backend.call("setting", json!({"key":"stealth","value":next}), Duration::from_secs(10)).is_ok() {
                let shown=app.clone();
                let _=app.run_on_main_thread(move || apply_stealth(&shown, next));
            } else { diag::log("command_failed", json!({"command": command})); }
        }
        (Some("open_workspace"), None) => { let shown=app.clone(); let _=app.run_on_main_thread(move || { let _=open_workspace(shown, None); }); }
        (Some("to_pet"), None) => { let shown=app.clone(); let _=app.run_on_main_thread(move || to_pet(&shown)); }
        (Some("panic"), None) => { let shown=app.clone(); let _=app.run_on_main_thread(move || stealth_panic(&shown)); }
        (Some("state"), None) => { let shown=app.clone(); let _=app.run_on_main_thread(move || diag::snapshot(&shown, "command")); }
        (Some("latency"), Some(ms)) => { if let Ok(ms)=ms.parse::<u64>() { diag::latency(app, ms.min(10_000), "command"); } }
        // Phase 42 (E-N2): the backend reads, through an AX worker it spawns, the TextEdit window whose title holds the
        // given fragment (a file the harness made); the answer is content-free (trust, counts, digests).
        (Some("ax_probe"), Some(fragment)) => {
            let backend=Arc::clone(&*app.state::<Arc<Backend>>());
            let answer=backend.call("diag_ax_probe", json!({"bundle_id":"com.apple.TextEdit","title_contains":fragment}), Duration::from_secs(60));
            match answer { Ok(facts) => diag::log("ax_probe", facts), Err(error) => diag::log("ax_probe_failed", json!({"error": error})) }
        }
        _ => diag::log("command_unknown", json!({"command": command})),
    }
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
                quit_guard::install(); // F177
            }
            let _ = app.global_shortcut().register("CommandOrControl+Shift+Space");
            let _ = app.global_shortcut().register("Control+Alt+Command+S");
            let _ = app.global_shortcut().register("Control+Alt+Command+H");
            use tauri::menu::{CheckMenuItem,Menu,MenuItem,PredefinedMenuItem};
            // Phase 33: the menu-bar icon is a template glyph (it changes shape while Stealth Lockdown is on) and its
            // menu carries the switch and Hide Pebble now; with Pebble out of the Dock and ⌘Tab it is the way back in.
            let stealth=CheckMenuItem::with_id(app,"stealth","Stealth Lockdown",true,false,Some("Control+Alt+Command+S"))?;
            let hide=MenuItem::with_id(app,"hide","Hide Pebble now",true,Some("Control+Alt+Command+H"))?;
            let show=MenuItem::with_id(app,"show","Open Pebble",true,Some("CommandOrControl+Shift+Space"))?;
            // Phase 45 (F079): a new conversation and Settings from the icon, run by the workspace as its menu items are
            let new_chat=MenuItem::with_id(app,"menu:new_chat","New Conversation",true,None::<&str>)?;
            let settings=MenuItem::with_id(app,"menu:settings","Settings…",true,None::<&str>)?;
            let quit=MenuItem::with_id(app,"quit","Quit Pebble",true,None::<&str>)?;
            let menu=Menu::with_items(app,&[&stealth,&hide,&PredefinedMenuItem::separator(app)?,&show,&new_chat,&settings,
                                            &PredefinedMenuItem::separator(app)?,&quit])?;
            app.manage(StealthItem(Mutex::new(Some(stealth.clone()))));
            let glyph=tauri::image::Image::from_bytes(include_bytes!("../icons/tray-off.png"))?;
            let _tray=tauri::tray::TrayIconBuilder::with_id("pebble").icon(glyph).icon_as_template(true).tooltip("Pebble").menu(&menu).on_menu_event(|app,event| match event.id.as_ref() {
                "stealth"=>stealth_toggle(app),"hide"=>hide_now(app),"show"=>reveal(app),"quit"=>request_quit(app),
                id if id.starts_with("menu:")=>menu_event(app,event.clone()),
                _=>{}
            }).build(app)?;
            #[cfg(target_os="macos")]
            if let Some(tray)=app.tray_by_id("pebble") { tray_label(&tray, false); } // F035
            // Phase 45 (F025, F026): the menu bar carries Pebble's own actions with their keys, and Help a way in.
            app.set_menu(app_menu(app.handle())?)?;
            app.on_menu_event(menu_event);
            configure_pet(app.handle())?;
            #[cfg(target_os="macos")]
            let _ = hide_dock_in_fullscreen(); // the windows exist, so tao's delegate class is registered
            #[cfg(target_os="macos")]
            space_anchor::place(app.handle(), "launch"); // Phase 39 (D82): Stealth's workspace opens on the desktop; Phase 42: made on a desktop Space only
            #[cfg(target_os="macos")]
            companion_follows_spaces(app.handle()); // Phase 39 (D84): the companion rejoins a Space it was left out of
            #[cfg(target_os="macos")]
            companion_rejoin_after_launch(app.handle()); // Phase 40: and the Space Pebble started on
            diag::commands(app.handle(), diag_run); // Phase 39 (D81): only with PEBBLE_SHELL_DIAG
            diag::log("launch", json!({"version": env!("CARGO_PKG_VERSION")}));
            diag::snapshots_after(app.handle(), "launch", &[1500]);
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
            // Phase 45 (F034, measured 2026-10-26 03:31): ⌘W to the pebble, then a click on it, brought the workspace
            // back without reveal: the policy stayed Accessory, so the menu bar was another app's. The workspace in
            // focus is an ordinary app again whenever Stealth Lockdown is off.
            tauri::WindowEvent::Focused(true) if window.label()=="main" => {
                // Phase 45 (F034): shown without being asked for (AppKit, at a click on the pebble): it goes again.
                #[cfg(target_os="macos")]
                if !WORKSPACE_WANTED.load(Ordering::SeqCst) && !main_fullscreen(window.app_handle()) {
                    let _=window.hide();
                    diag::log("workspace_rehidden", json!({"why": "focused unasked"}));
                    return;
                }
                #[cfg(target_os="macos")]
                if !stealth_on() && !regular_policy() && window.is_visible().unwrap_or(false) {
                    let app=window.app_handle();
                    let _=app.set_activation_policy(tauri::ActivationPolicy::Regular);
                    companion_over_full_screen(app); // re-applied after the policy change, as reveal does
                    diag::log("policy_restored", json!({"why": "workspace focused"}));
                    menu_bar_handoff(app); // Phase 45 (F058): active before it became ordinary, so not yet its menu bar
                }
                sync_title_bar(window);
                pet_passthrough(window.app_handle(), true); // Phase 45 (F085)
            }
            tauri::WindowEvent::Focused(false) if window.label()=="main" => { pet_passthrough(window.app_handle(), false); sync_title_bar(window); }
            tauri::WindowEvent::Moved(_) if window.label()=="pet" => pet_moved(window.app_handle()), // Phase 45 (F062)
            // Phase 45 (F126, audit round R4): pass-through set over the full-screen workspace stayed on after it became a
            // window beside the pebble; it is decided again whenever the workspace moves or changes size
            tauri::WindowEvent::Resized(_) | tauri::WindowEvent::Moved(_) if window.label()=="main" => {
                pet_passthrough(window.app_handle(), true);
                sync_title_bar(window);
            }
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
