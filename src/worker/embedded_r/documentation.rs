use harp::object::RObject;
use libr::SEXP;

#[allow(clippy::result_large_err)]
#[harp::register]
pub extern "C-unwind" fn mcp_console_documentation_html(html: SEXP) -> harp::Result<SEXP> {
    let html = Vec::<u8>::try_from(&RObject::view(html))?;
    let convert = || -> anyhow::Result<String> {
        let html = std::str::from_utf8(&html)?;
        let config = html2text::config::plain().no_link_wrapping();
        let dom = config.parse_html(html.as_bytes())?;

        // html2text drops table captions. Move them before their tables so the
        // crate renders their text and inline markup alongside the table.
        let mut pending = vec![dom.document.clone()];
        while let Some(node) = pending.pop() {
            let mut children = node.children.borrow_mut();
            let mut lifted = Vec::new();
            for child in children.drain(..) {
                if matches!(&child.data, html2text::Element { name, .. }
                    if name.local.as_ref() == "table")
                {
                    lifted.extend(child.children.borrow_mut().extract_if(.., |caption| {
                        matches!(&caption.data, html2text::Element { name, .. }
                            if name.local.as_ref() == "caption")
                    }));
                }
                lifted.push(child);
            }
            *children = lifted;
            pending.extend(children.iter().cloned());
        }

        let tree = config.dom_to_render_tree(&dom)?;
        Ok(config.render_to_string(tree, 80)?)
    };
    let text = convert().map_err(|error| harp::anyhow!("{error}"))?;
    Ok(RObject::from(text).sexp)
}
