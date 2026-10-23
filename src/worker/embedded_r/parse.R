function(source) {
  tryCatch(
    {
      suppressWarnings(str2expression(source))
      NULL
    },
    error = conditionMessage
  )
}
