# Final native artifacts on an EL8 userspace; no checkout enters this context.
FROM ghcr.io/astral-sh/uv:0.13.1 AS uv
FROM almalinux:8-minimal AS standalone
RUN microdnf -y --setopt=install_weak_deps=0 install tar gzip && microdnf clean all
WORKDIR /empty-workspace

FROM standalone AS no-r
ENV UV_NO_CONFIG=1 \
    UV_PYTHON_INSTALL_DIR=/opt/console-cache/mcp-console/dependencies/uv/python
COPY --from=uv /uv /usr/local/bin/uv
RUN microdnf -y --setopt=install_weak_deps=0 install binutils && microdnf clean all
RUN uv python install 3.14 \
    && ln -s "$(uv python find --managed-python 3.14)" /usr/local/bin/python3
ENV UV_PYTHON_DOWNLOADS=never UV_TOOL_DIR=/tmp/tools UV_TOOL_BIN_DIR=/tmp/bin UV_CACHE_DIR=/tmp/uv-cache
ENV XDG_CACHE_HOME=/opt/console-cache

# EPEL's R-core includes R's recommended packages on both architectures.
FROM no-r AS r-runtime
RUN microdnf -y --setopt=install_weak_deps=0 install epel-release \
    && microdnf -y --enablerepo=powertools --setopt=install_weak_deps=0 install \
    R-core libcurl openssl-libs libxml2 cairo fontconfig harfbuzz fribidi \
    libjpeg-turbo libpng libtiff libuv libwebp && microdnf clean all
COPY configure-ci-r.R /tmp/configure-ci-r.R
RUN Rscript --vanilla /tmp/configure-ci-r.R && rm /tmp/configure-ci-r.R

FROM r-runtime AS r-preparation
RUN microdnf -y --enablerepo=powertools --setopt=install_weak_deps=0 install \
    R-core-devel gcc gcc-c++ gcc-gfortran make pkgconf-pkg-config curl \
    libcurl-devel openssl-devel libxml2-devel cairo-devel fontconfig-devel \
    harfbuzz-devel fribidi-devel libjpeg-turbo-devel libpng-devel libtiff-devel \
    libuv-devel libwebp-devel && microdnf clean all
# Match the documented Console cache layout, without opting into host caches.
ENV IR_CACHE_DIR=/opt/console-cache/mcp-console/dependencies/ir \
    IR_LIBRARY_ROOT=/opt/console-cache/mcp-console/dependencies/ir/libraries \
    R_USER_CACHE_DIR=/opt/console-cache/mcp-console/dependencies \
    RENV_PATHS_ROOT=/opt/console-cache/mcp-console/dependencies/renv \
    PKG_CACHE_DIR=/opt/console-cache/mcp-console/dependencies/R/pkgcache \
    R_PKG_CACHE_DIR=/opt/console-cache/mcp-console/dependencies \
    UV_CACHE_DIR=/opt/console-cache/mcp-console/dependencies/uv/cache
ENV LIBARROW_BINARY=true MAKEFLAGS=-j1
ENV R_HOME=/usr/lib64/R
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
RUN test -n "$R_PREPARATION_RUN" && uv tool run --isolated --python 3.14 --from r-lib-ir ir run \
    --rscript "$R_HOME/bin/Rscript" \
    --with tidyverse --with dplyr --with dbplyr --with reticulate --with DBI \
    --with duckdb --with nanoarrow --with yyjsonr --with ggplot2 \
    --with jsonlite --with pillar --with tibble --with utf8 \
    --isolated --vanilla -e 'sessionInfo()'
# Preserve the prepared dependency cache; CMake is only a preparation tool.
RUN uv cache clean cmake

FROM r-runtime AS with-r
COPY --from=r-preparation /opt/console-cache /opt/console-cache
