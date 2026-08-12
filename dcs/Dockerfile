FROM python:3.11-slim

WORKDIR /app

# do-mpc/casadi need a C/C++ runtime toolchain to build some deps on some
# platforms; keep the image lean but include what pip needs.
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY water_plant_DCS/requirements.txt ./water_plant_DCS/requirements.txt
RUN pip install --no-cache-dir -r water_plant_DCS/requirements.txt

# The dcs process imports plantbus/, reference/, and tags.yaml as siblings
# of dcs/ (see dcs/main.py, dcs/controller_wrapper.py sys.path handling),
# so the whole water_plant_DCS/ tree is copied in, not just dcs/.
COPY water_plant_DCS/ ./water_plant_DCS/

WORKDIR /app/water_plant_DCS

ENV PLC_ENDPOINTS=""
ENV DCS_SERVER_ENDPOINT="opc.tcp://0.0.0.0:4850/"
ENV DCS_CYCLE_S="1.0"
ENV DCS_SOLVE_BUDGET_S="2.0"
ENV DCS_HEARTBEAT_STALL_CYCLES="5"
ENV DCS_HISTORIAN_DB="/data/dcs_historian.sqlite3"
# Per-unit setpoint source (cycle CSV or constant target, see
# dcs/config.py's unit_setpoint_sources) defaults to Unit1=step cycle,
# Unit2=ramp cycle, Unit3=constant, with no env vars required. Override
# per unit with DCS_UNIT<n>_CYCLE=<path relative to water_plant_DCS/> or
# DCS_UNIT<n>_TARGET_M=<value> if needed.

VOLUME ["/data"]
EXPOSE 4850

CMD ["python", "dcs/main.py"]
