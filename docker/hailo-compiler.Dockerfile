# syntax=docker/dockerfile:1.7

FROM ubuntu:22.04

ARG DEBIAN_FRONTEND=noninteractive
ARG HAILO_UID=1000
ARG HAILO_GID=1000

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VIRTUAL_ENV=/venv \
    HAILO_SDK_ROOT=/opt/hailo \
    PATH=/venv/bin:/opt/hailo/usr/bin:${PATH} \
    LD_LIBRARY_PATH=/opt/hailo/usr/lib:/opt/hailo/usr/lib/x86_64-linux-gnu

LABEL org.opencontainers.image.title="TradingBot Hailo-8 Compiler" \
      org.opencontainers.image.description="x86_64 Hailo Dataflow Compiler 3.34.0 environment adapted for TradingBot"

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      build-essential \
      bsdmainutils \
      ca-certificates \
      curl \
      dpkg-dev \
      graphviz \
      libatlas-base-dev \
      libgl1 \
      libglib2.0-0 \
      libgomp1 \
      libgraphviz-dev \
      liblapack-dev \
      lsb-release \
      pkg-config \
      python3.10 \
      python3.10-dev \
      python3.10-venv \
      python3-distutils \
      python3-pip \
      python3-tk && \
    rm -rf /var/lib/apt/lists/*

RUN python3.10 -m venv "${VIRTUAL_ENV}" && \
    "${VIRTUAL_ENV}/bin/python" -m pip install --no-cache-dir --upgrade pip setuptools wheel

RUN --mount=type=bind,from=hailo_packages,source=compiler.whl,target=/tmp/hailo_dataflow_compiler-3.34.0-py3-none-linux_x86_64.whl,ro \
    "${VIRTUAL_ENV}/bin/python" -m pip install --no-cache-dir \
      /tmp/hailo_dataflow_compiler-3.34.0-py3-none-linux_x86_64.whl

RUN --mount=type=bind,from=hailo_packages,source=hailort.deb,target=/tmp/hailort.deb,ro \
    test "$(dpkg-deb -f /tmp/hailort.deb Architecture)" = "amd64" && \
    mkdir -p "${HAILO_SDK_ROOT}" && \
    dpkg-deb -x /tmp/hailort.deb "${HAILO_SDK_ROOT}" && \
    hailort_library="$(find "${HAILO_SDK_ROOT}/usr/lib" -name 'libhailort.so*' -print -quit)" && \
    test -n "${hailort_library}" && \
    ln -s "${hailort_library}" /usr/local/lib/libhailort.so && \
    test -x "${HAILO_SDK_ROOT}/usr/bin/hailortcli" && \
    ln -s "${HAILO_SDK_ROOT}/usr/bin/hailortcli" /usr/local/bin/hailortcli && \
    ldconfig

RUN groupadd --gid "${HAILO_GID}" hailo && \
    useradd --uid "${HAILO_UID}" --gid "${HAILO_GID}" --create-home \
      --shell /bin/bash hailo && \
    install -d -o hailo -g hailo /workspace /venv/etc/hailo && \
    chown hailo:hailo /venv/bin /venv/bin/activate

WORKDIR /workspace
USER hailo

CMD ["bash"]
