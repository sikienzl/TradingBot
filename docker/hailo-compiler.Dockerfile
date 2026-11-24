# syntax=docker/dockerfile:1.7
FROM ubuntu:22.04

ARG DEBIAN_FRONTEND=noninteractive
ARG HAILO_UID=1000
ARG HAILO_GID=1000
ENV VIRTUAL_ENV=/venv \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HAILO_SDK_ROOT=/opt/hailo \
    PATH=/venv/bin:/opt/hailo/usr/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    LD_LIBRARY_PATH=/opt/hailo/usr/lib:/opt/hailo/usr/lib/x86_64-linux-gnu

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential bsdmainutils ca-certificates curl dpkg-dev graphviz \
    libatlas-base-dev libgl1 libglib2.0-0 libgomp1 libgraphviz-dev \
    liblapack-dev lsb-release pkg-config python3.10 python3.10-dev \
    python3.10-venv python3-distutils python3-pip python3-tk \
    && rm -rf /var/lib/apt/lists/*

# The proprietary SDK files come from a separate local build context and are
# mounted for installation, rather than copied as distributable image layers.
RUN --mount=type=bind,from=hailo_sdk,source=hailo_dataflow_compiler-3.34.0-py3-none-linux_x86_64.whl,target=/tmp/dfc.whl \
    --mount=type=bind,from=hailo_sdk,source=hailort_4.24.0_amd64.deb,target=/tmp/hailort.deb \
    python3.10 -m venv "$VIRTUAL_ENV" \
    && "$VIRTUAL_ENV/bin/python" -m pip install --no-cache-dir --upgrade pip setuptools wheel \
    && ln -s /tmp/dfc.whl /tmp/hailo_dataflow_compiler-3.34.0-py3-none-linux_x86_64.whl \
    && "$VIRTUAL_ENV/bin/python" -m pip install --no-cache-dir /tmp/hailo_dataflow_compiler-3.34.0-py3-none-linux_x86_64.whl \
    && rm /tmp/hailo_dataflow_compiler-3.34.0-py3-none-linux_x86_64.whl \
    && test "$(dpkg-deb -f /tmp/hailort.deb Architecture)" = amd64 \
    && mkdir -p "$HAILO_SDK_ROOT" \
    && dpkg-deb -x /tmp/hailort.deb "$HAILO_SDK_ROOT" \
    && hailort_library="$(find "$HAILO_SDK_ROOT/usr/lib" -name 'libhailort.so*' -print -quit)" \
    && test -n "$hailort_library" \
    && ln -s "$hailort_library" /usr/local/lib/libhailort.so \
    && test -x "$HAILO_SDK_ROOT/usr/bin/hailortcli" \
    && ln -s "$HAILO_SDK_ROOT/usr/bin/hailortcli" /usr/local/bin/hailortcli \
    && ldconfig

RUN groupadd --gid "$HAILO_GID" hailo \
    && useradd --uid "$HAILO_UID" --gid "$HAILO_GID" --create-home --shell /bin/bash hailo \
    && install -d -o hailo -g hailo /workspace /venv/etc/hailo \
    && chown hailo:hailo /venv/bin /venv/bin/activate

COPY scripts/hailo_compile_onnx.py /opt/hailo/hailo_compile_onnx.py
WORKDIR /workspace
USER hailo
CMD ["bash"]
