# Native wheel validation on the supported userspace; no checkout enters here.
FROM ghcr.io/astral-sh/uv:0.12.22 AS uv
FROM ubuntu:22.04 AS base
ENV DEBIAN_FRONTEND=noninteractive UV_NO_CONFIG=1 \
    UV_PYTHON_INSTALL_DIR=/opt/console-cache/mcp-console/dependencies/uv/python
COPY --from=uv /uv /usr/local/bin/uv
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates libcap2 libgcc-s1 libstdc++6 binutils \
    && rm -rf /var/lib/apt/lists/*
RUN uv python install 3.13 \
    && ln -s "$(uv python find --managed-python 3.13)" /usr/local/bin/python3
ENV UV_PYTHON_DOWNLOADS=never UV_TOOL_DIR=/tmp/tools UV_TOOL_BIN_DIR=/tmp/bin UV_CACHE_DIR=/tmp/uv-cache
ENV XDG_CACHE_HOME=/opt/console-cache
WORKDIR /empty-workspace

FROM base AS no-r

# R and its package preparation are independent of Console's native libraries.
# Jammy's stock R 4.1 cannot install the current default Arrow/DuckDB packages.
FROM base AS r-repository
RUN apt-get update && apt-get install -y --no-install-recommends curl gnupg \
    && curl -fsSL https://cloud.r-project.org/bin/linux/ubuntu/marutter_pubkey.asc -o /etc/apt/trusted.gpg.d/cran.asc \
    && test "$(gpg --show-keys --with-colons /etc/apt/trusted.gpg.d/cran.asc | awk -F: '$1 == "fpr" {print $10; exit}')" = E298A3A825C0D65DFD57CBB651716619E084DAB9 \
    && printf '%s\n' 'deb [signed-by=/etc/apt/trusted.gpg.d/cran.asc] https://cloud.r-project.org/bin/linux/ubuntu jammy-cran40/' > /etc/apt/sources.list.d/cran.list

FROM base AS r-runtime
COPY --from=r-repository /etc/apt/trusted.gpg.d/cran.asc /etc/apt/trusted.gpg.d/cran.asc
COPY --from=r-repository /etc/apt/sources.list.d/cran.list /etc/apt/sources.list.d/cran.list
RUN apt-get update && apt-get install -y --no-install-recommends \
    r-base-core libcurl4 libssl3 libxml2 libcairo2 libfontconfig1 \
    libharfbuzz0b libfribidi0 libjpeg-turbo8 libpng16-16 libtiff5 libuv1 libwebp7 libwebpmux3 \
    && rm -rf /var/lib/apt/lists/*
COPY configure-ci-r.R /tmp/configure-ci-r.R
RUN Rscript --vanilla /tmp/configure-ci-r.R && rm /tmp/configure-ci-r.R

FROM r-runtime AS r-preparation
RUN apt-get update && apt-get install -y --no-install-recommends \
    r-base-dev build-essential pkgconf curl libcurl4-openssl-dev libssl-dev \
    libxml2-dev libcairo2-dev libfontconfig1-dev libharfbuzz-dev libfribidi-dev \
    libjpeg-dev libpng-dev libtiff-dev libuv1-dev libwebp-dev \
    && rm -rf /var/lib/apt/lists/*
# Match the documented Console cache layout, without opting into host caches.
ENV IR_CACHE_DIR=/opt/console-cache/mcp-console/dependencies/ir \
    IR_LIBRARY_ROOT=/opt/console-cache/mcp-console/dependencies/ir/libraries \
    R_USER_CACHE_DIR=/opt/console-cache/mcp-console/dependencies \
    RENV_PATHS_ROOT=/opt/console-cache/mcp-console/dependencies/renv \
    PKG_CACHE_DIR=/opt/console-cache/mcp-console/dependencies/R/pkgcache \
    R_PKG_CACHE_DIR=/opt/console-cache/mcp-console/dependencies \
    UV_CACHE_DIR=/opt/console-cache/mcp-console/dependencies/uv/cache
ENV LIBARROW_BINARY=true MAKEFLAGS=-j1
ENV R_HOME=/usr/lib/R
# Bound DuckDB's source-build memory in the language preparation fixture.
# These flags affect test dependencies, never the distributed wheel binaries.
RUN printf '%s\n' 'CXX17FLAGS=-O0 -g0' > /opt/preparation.Makevars
ENV R_MAKEVARS_USER=/opt/preparation.Makevars
RUN uv venv /opt/cmake && uv pip install --python /opt/cmake/bin/python cmake
ENV PATH=/opt/cmake/bin:$PATH
# Latest-package markers expire even when Docker's parent layers stay cached.
# Refresh resolution for each driver run, retaining cached system/tool setup.
# Match Console's complete runtime request and R_HOME cache identity.
ARG R_PREPARATION_RUN
RUN test -n "$R_PREPARATION_RUN" && uv tool run --isolated --python 3.13 --from r-lib-ir ir run \
    --rscript "$R_HOME/bin/Rscript" \
    --with tidyverse --with dplyr --with dbplyr --with reticulate --with DBI \
    --with duckdb --with arrow --with nanoarrow --with yyjsonr --with ggplot2 \
    --with jsonlite --with pillar --with tibble --with utf8 \
    --isolated --vanilla -e 'sessionInfo()'
# Preserve the prepared dependency cache; CMake is only a preparation tool.
RUN uv cache clean cmake

FROM r-runtime AS with-r
COPY --from=r-preparation /opt/console-cache /opt/console-cache
