/* A different runtime with CPython symbols, not the selected interpreter. */
const char *Py_GetVersion(void) { return "3.9.0 (other build)"; }
int Py_IsInitialized(void) { return 0; }
