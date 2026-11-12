// Gives DualLink.exe its icon and version details (only when building on Windows).
fn main() {
    println!("cargo:rerun-if-changed=../assets/icon.ico");
    println!("cargo:rerun-if-env-changed=DUALLINK_REPO");
    if cfg!(windows) && std::env::var("CARGO_CFG_TARGET_OS").as_deref() == Ok("windows") {
        let mut res = winresource::WindowsResource::new();
        res.set_icon("../assets/icon.ico");
        res.set("ProductName", "DualLink");
        res.set("FileDescription", "DualLink");
        if let Err(e) = res.compile() {
            println!("cargo:warning=no icon for DualLink.exe: {e}");
        }
    }
}
