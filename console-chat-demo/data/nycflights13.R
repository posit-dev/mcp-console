# Regenerate data/nycflights13/*.parquet from the nycflights13 R package.
# Run from console-chat-demo/: Rscript data/nycflights13.R
stopifnot(file.exists("data/nycflights13.R"))
con <- DBI::dbConnect(duckdb::duckdb())
DBI::dbExecute(con, "SET TimeZone = 'UTC'")
dir.create("data/nycflights13", showWarnings = FALSE)
for (name in c("airlines", "airports", "flights", "planes", "weather")) {
  duckdb::duckdb_register(con, "source", getExportedValue("nycflights13", name))
  columns <- DBI::dbListFields(con, "source")
  # time_hour is a POSIXct in New York time; DuckDB receives it as UTC.
  # Store local time so it agrees with the year, month, day and hour columns.
  if ("time_hour" %in% columns) {
    columns[columns == "time_hour"] <-
      "timezone('America/New_York', time_hour::TIMESTAMPTZ) AS time_hour"
  }
  DBI::dbExecute(
    con,
    sprintf(
      "COPY (SELECT %s FROM source) TO 'data/nycflights13/%s.parquet'
       (FORMAT parquet, COMPRESSION zstd, COMPRESSION_LEVEL 19)",
      paste(columns, collapse = ", "),
      name
    )
  )
  duckdb::duckdb_unregister(con, "source")
}
DBI::dbDisconnect(con)
