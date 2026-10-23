fn main() {
    tauri_build::try_build(tauri_build::Attributes::new().app_manifest(
        tauri_build::AppManifest::new().commands(&[
            "rpc", "compact", "open_workspace", "pet_expand", "pet_gaze", "show_pet", "stealth_set",
            "login_item_status", "login_item_set", "notification_authorization", "service_restart",
            "export_choose_destination", "telemetry_choose_destination",
        ]),
    )).expect("Could not build Pebble permissions");
}
