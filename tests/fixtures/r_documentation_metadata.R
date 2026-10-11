library <- commandArgs(trailingOnly = TRUE)[[1L]]
metadata <- file.path(library, "consolehelp", "Meta", "vignette.rds")
vignettes <- readRDS(metadata)
# Native metadata can name a rendered artifact independently of its source.
vignettes$PDF[vignettes$File == "rendered.Rnw"] <- "evaluated.HTML"
vignettes$R[vignettes$File == "rendered.Rnw"] <- "rendered.R"
vignettes$PDF[vignettes$File == "pdf.Rnw"] <- "prebuilt.pdf"
vignettes$R[vignettes$File == "pdf.Rnw"] <- "pdf.R"
vignettes$PDF[vignettes$File == "markdown.Rnw"] <- "prebuilt.md"
vignettes$R[vignettes$File == "markdown.Rnw"] <- "markdown.R"
saveRDS(vignettes, metadata)
