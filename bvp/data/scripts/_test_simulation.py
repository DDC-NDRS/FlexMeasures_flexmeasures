"""Tests a small simulation against the BVP running on a server."""
from datetime import timedelta

from isodate import parse_datetime

from bvp.data.scripts.simulation_utils import (
    check_version,
    check_services,
    get_auth_token,
    get_connections,
    post_meter_data,
    post_price_forecasts,
    post_weather_data,
)


# Setup
server = "local"

if server == "play":
    host = "https://play.a1-bvp.com"
elif server == "staging":
    host = "https://staging.a1-bvp.com"
else:
    host = "http://localhost:5000"

latest_version = check_version(host)
services = check_services(host, latest_version)
auth_token = get_auth_token(host, "solar@seita.nl", "solar")
connections = get_connections(host, latest_version, auth_token)

# Initialisation
num_days = 50
sim_start = "2018-01-01T00:00:00+09:00"
post_price_forecasts(
    host, latest_version, auth_token, start=parse_datetime(sim_start), num_days=num_days
)
post_weather_data(
    host, latest_version, auth_token, start=parse_datetime(sim_start), num_days=num_days
)
post_meter_data(
    host,
    latest_version,
    auth_token,
    start=parse_datetime(sim_start),
    num_days=num_days,
    connection=connections[1],
)

# Run forecasting jobs
input(
    "Run all forecasting jobs, then press Enter to continue ...\n"
    "You can run this in another bvp-venv shell:\n\n"
    "flask run_worker --name 'Sim worker' --queue 'forecasting'\n"
)

# Steps
num_steps = 10
for i in range(num_steps):
    print("Running step %s out of %s" % (i, num_steps))

    post_weather_data(
        host,
        latest_version,
        auth_token,
        start=parse_datetime(sim_start) + timedelta(days=num_days + i),
        num_days=1,
    )
    post_meter_data(
        host,
        latest_version,
        auth_token,
        start=parse_datetime(sim_start) + timedelta(days=num_days + i),
        num_days=1,
        connection=connections[1],
    )

    # Run forecasting jobs
    input("Run all forecasting jobs again and press enter to continue..\n")
