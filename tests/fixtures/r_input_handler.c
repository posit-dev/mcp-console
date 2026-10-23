#include <R.h>
#include <R_ext/Rdynload.h>
#include <R_ext/eventloop.h>
#include <Rinternals.h>
#include <Rinterface.h>

#include <fcntl.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

/* R exports this wrapper, but Rinterface.h declares only its callback pointer. */
extern int R_ReadConsole(const char *, unsigned char *, int, int);

#define TEST_CONSOLE_BUFFER_SIZE 4

static InputHandler *registered_handler = NULL;
static int registered_fd = -1;
static SEXP registered_callback = NULL;

static void handle_input(void *user_data) {
  (void)user_data;

  int fd = registered_fd;
  InputHandler *handler = registered_handler;
  registered_fd = -1;
  registered_handler = NULL;
  removeInputHandler(&R_InputHandlers, handler);

  char byte;
  ssize_t length = read(fd, &byte, 1);
  close(fd);
  if (length != 1) {
    Rf_error("failed to read test input handler byte");
  }

  SEXP callback = PROTECT(registered_callback);
  registered_callback = NULL;
  R_ReleaseObject(callback);
  SEXP call = PROTECT(Rf_lang1(callback));
  Rf_eval(call, R_GlobalEnv);
  UNPROTECT(2);
}

static SEXP register_input_handler(SEXP path, SEXP callback) {
  if (registered_handler != NULL) {
    Rf_error("test input handler is already registered");
  }

  const char *fifo = CHAR(STRING_ELT(path, 0));
  if (mkfifo(fifo, S_IRUSR | S_IWUSR) < 0) {
    Rf_error("failed to create test input handler FIFO");
  }

  int fd = open(fifo, O_RDONLY | O_NONBLOCK);
  if (fd < 0) {
    Rf_error("failed to open test input handler FIFO");
  }

  InputHandler *handler =
      addInputHandler(R_InputHandlers, fd, handle_input, XActivity);
  if (handler == NULL) {
    close(fd);
    Rf_error("failed to register test input handler");
  }
  registered_fd = fd;
  registered_handler = handler;
  registered_callback = callback;
  R_PreserveObject(registered_callback);
  return R_NilValue;
}

static SEXP read_console_once(SEXP prompt) {
  unsigned char buffer[TEST_CONSOLE_BUFFER_SIZE];
  int status = R_ReadConsole(
      CHAR(STRING_ELT(prompt, 0)),
      buffer,
      sizeof(buffer),
      0
  );
  if (status <= 0) {
    return R_NilValue;
  }
  return Rf_mkString((const char *)buffer);
}

static SEXP read_console_line(SEXP prompt) {
  unsigned char buffer[TEST_CONSOLE_BUFFER_SIZE];
  for (;;) {
    int status = R_ReadConsole(
        CHAR(STRING_ELT(prompt, 0)),
        buffer,
        sizeof(buffer),
        0
    );
    if (status <= 0 || strchr((const char *)buffer, '\n') != NULL) {
      return R_NilValue;
    }
  }
}

static const R_CallMethodDef call_methods[] = {
    {"mcp_test_register_input_handler", (DL_FUNC)&register_input_handler, 2},
    {"mcp_test_read_console_once", (DL_FUNC)&read_console_once, 1},
    {"mcp_test_read_console_line", (DL_FUNC)&read_console_line, 1},
    {NULL, NULL, 0},
};

void R_init_mcp_test_input_handler(DllInfo *dll) {
  R_registerRoutines(dll, NULL, call_methods, NULL, NULL);
  R_useDynamicSymbols(dll, FALSE);
}
