#define Py_LIMITED_API 0x03090000
#include <Python.h>

static int optional_import_absent(void) {
    PyObject *dependency = PyImport_ImportModule(
        "mcp_console_test_native_optional_dependency");
    int fallback = dependency == NULL;
    if (fallback) {
        if (!PyErr_ExceptionMatches(PyExc_ImportError)) {
            return -1;
        }
        PyErr_Clear();
    } else {
        Py_DECREF(dependency);
    }
    return fallback;
}

static PyObject *probe(PyObject *self, PyObject *unused) {
    (void)self;
    (void)unused;
    int fallback = optional_import_absent();
    return fallback < 0 ? NULL : PyBool_FromLong(fallback);
}

static PyMethodDef methods[] = {
    {"probe", probe, METH_NOARGS, NULL},
    {NULL, NULL, 0, NULL},
};

static struct PyModuleDef module = {
    PyModuleDef_HEAD_INIT,
    .m_name = "mcp_console_test_native_optional",
    .m_size = -1,
    .m_methods = methods,
};

PyMODINIT_FUNC PyInit_mcp_console_test_native_optional(void) {
    int fallback = optional_import_absent();
    if (fallback < 0) {
        return NULL;
    }

    PyObject *result = PyModule_Create(&module);
    if (result == NULL) {
        return NULL;
    }
    if (PyModule_AddIntConstant(result, "fallback", fallback) < 0) {
        Py_DECREF(result);
        return NULL;
    }
    return result;
}
