// A present R launcher that succeeds without reporting an installation.
fn main() {
    assert_eq!(std::env::args().skip(1).collect::<Vec<_>>(), ["RHOME"]);
}
