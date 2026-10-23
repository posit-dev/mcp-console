typedef void (*new_page_fn)(void *context, void *device);
typedef void (*close_fn)(void *device);

extern new_page_fn mcp_console_graphics_original_new_page(void *device);
extern close_fn mcp_console_graphics_original_close(void *device);
extern void mcp_console_graphics_did_new_page(void *device);
extern void mcp_console_graphics_did_close(void *device);

/*
 * R graphics callbacks may raise an R error and longjmp. Forward them from C
 * so that jump never crosses a live Rust frame, then notify Rust only after a
 * callback returns normally.
 */
void mcp_console_graphics_new_page(void *context, void *device) {
    new_page_fn callback = mcp_console_graphics_original_new_page(device);
    callback(context, device);
    mcp_console_graphics_did_new_page(device);
}

void mcp_console_graphics_close(void *device) {
    close_fn callback = mcp_console_graphics_original_close(device);
    callback(device);
    mcp_console_graphics_did_close(device);
}
