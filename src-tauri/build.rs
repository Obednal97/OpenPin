fn main() {
    // commands::EXPECTED_TEAM_ID reads it with option_env!.
    println!("cargo:rerun-if-env-changed=APPLE_TEAM_ID");
    tauri_build::build()
}
