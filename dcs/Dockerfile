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
ENV DCS_SERVER_ENDPOINT="opc.tcp://0.0.0.0:4900/"
ENV DCS_CYCLE_S="1.0"
ENV DCS_SOLVE_BUDGET_S="2.0"
ENV DCS_HEARTBEAT_STALL_CYCLES="5"
ENV DCS_HISTORIAN_DB="/data/dcs_historian.sqlite3"
ENV DCS_TARGET_LEVEL_M="0.15"

VOLUME ["/data"]
EXPOSE 4900

CMD ["python", "dcs/main.py"]
