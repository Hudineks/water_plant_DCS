# Build from the water_plant_DCS/ directory (this service needs the
# plantbus/ package and tags.yaml, which live one level above plc/):
#
#   docker build -f plc/Dockerfile -t water-plant-plc .
#   docker run -e UNIT_ID=1 -e OPCUA_PORT=4840 -p 4840:4840 water-plant-plc
#
FROM python:3.11-slim

WORKDIR /app

# Only the two packages this service actually imports: asyncua for the OPC
# UA server, PyYAML because plantbus/contract.py parses tags.yaml.
RUN pip install --no-cache-dir asyncua>=1.1.0 PyYAML>=6.0

COPY tags.yaml ./tags.yaml
COPY plantbus ./plantbus
COPY plc ./plc

ENV UNIT_ID=1
ENV OPCUA_PORT=4840
ENV TIME_SCALE=1.0

EXPOSE 4840

CMD ["python", "-m", "plc.unit"]
