FROM python:3.11-slim

WORKDIR /app

# Install only what hmi/ needs to run; the repo-wide requirements.txt also
# pulls in casadi/do-mpc for dcs/, which this image does not need.
RUN pip install --no-cache-dir \
    asyncua>=1.1.0 \
    PyYAML>=6.0 \
    fastapi>=0.110.0 \
    "uvicorn[standard]>=0.29.0" \
    websockets>=12.0 \
    pydantic>=2.0

# tags.yaml and plantbus/ are the frozen contract shared by plc/, dcs/, hmi/.
COPY tags.yaml ./tags.yaml
COPY plantbus ./plantbus
COPY hmi ./hmi

EXPOSE 8080

ENV PLC_ENDPOINTS="opc.tcp://plc-1:4840/,opc.tcp://plc-2:4840/,opc.tcp://plc-3:4840/"
ENV DCS_ENDPOINT="opc.tcp://dcs:4850/"

CMD ["python", "-m", "uvicorn", "hmi.main:app", "--host", "0.0.0.0", "--port", "8080"]
