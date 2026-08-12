# Weekly trip clustering, on the Synology beside Immich.
#
# python:3.12 rather than the 3.9 the Mac was stuck on — reverse_geocoder pulls
# numpy and scipy, and 3.12 has prebuilt wheels for both on linux/amd64. If the
# NAS is ARM there may be no wheel and pip will try to compile; check the build
# log before assuming it hung.

FROM python:3.12-slim

# reverse_geocoder unpacks its dataset on first use and wants a writable HOME.
ENV HOME=/tmp PYTHONUNBUFFERED=1

WORKDIR /app

# Requirements first so a code change doesn't re-download numpy/scipy.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY tripcluster/ ./tripcluster/
COPY tools/ ./tools/
COPY mapdata/ ./mapdata/
COPY cluster_trips.py ./
COPY deploy/tripcluster/run.sh /usr/local/bin/run.sh
COPY deploy/tripcluster/notify.py /usr/local/bin/notify.py
RUN chmod +x /usr/local/bin/run.sh

# config.yaml and trips.db are mounted, not baked: config carries your homes,
# and trips.db carries the album ids and any custom_name you have typed by
# hand. Losing trips.db means the next run creates duplicate albums.

ENTRYPOINT ["/usr/local/bin/run.sh"]
