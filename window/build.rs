fn main() {
    let config = slint_build::CompilerConfiguration::new().with_style("fluent".into());
    slint_build::compile_with_config("ui/main.slint", config).expect("the window's design compiles");
    println!("cargo:rerun-if-changed=../assets/icon.ico");
    if cfg!(windows) && std::env::var("CARGO_CFG_TARGET_OS").as_deref() == Ok("windows") {
        let mut res = winresource::WindowsResource::new();
        res.set_icon("../assets/icon.ico");
        res.set("ProductName", "DualLink");
        res.set("FileDescription", "DualLink");
        if let Err(e) = res.compile() {
            println!("cargo:warning=no icon for DualLinkWindow.exe: {e}");
        }
    }
}
