# Prometheus NMOS BCP-008 Exporter

[![Docker Pulls](https://img.shields.io/docker/pulls/tischr/nmos-exporter)](https://hub.docker.com/r/tischr/nmos-exporter)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/license/MIT)

A Prometheus Exporter that queries BCP-008 sender and receiver information of NMOS nodes using IS-12, and exposes them as Prometheus metrics. 

## Installation

### Docker

Pull and run the docker image from dockerhub. An example docker-compose.yml, including Prometheus can be found under [examples/](examples/).

```bash
docker run -p 9080:9080 tischr/nmos-exporter
```
Or build the image locally 

```bash
git clone git@github.com:tischr/nmos-exporter.git && cd ./nmos-exporter
docker build -t tischr/nmos-exporter:latest .
cd ./examples
docker compose up -d 
```

### Python

If you prefer to run the exporter without Docker, you can run it directly with Python. 

Requirements: 
* Python 3.12+

```bash
git clone git@github.com:tischr/nmos-exporter.git && cd ./nmos-exporter
python -m venv .venv
source .venv/bin/activate
pip install requirements.txt
python exporter.py
```

This option is useful for local development, or envrionments where Docker is not available.

## Usage

The exporter queries the NMOS nodes at scrape time, using the target parameter (similar to the snmp-exporter). An example prometheus.yml can be found under [examples/prometheus.yml](examples/prometheus.yml). 

```yml
scrape_configs:
  - job_name: "nmos-exporter"
    metrics_path: /probe
    static_configs:
      - targets:
          - nmos-node-1:8080
          - nmos-node-2:8080
    relabel_configs:
      - source_labels: [__address__]
        target_label: __param_target
      - source_labels: [__param_target]
        target_label: instance
      - target_label: __address__
        replacement: nmos-exporter:9080
```

You can test the exporter against the [nmos-device-control-mock](https://github.com/AMWA-TV/nmos-device-control-mock). 

## Contributing

Pull requests are welcome.

## License

This project is licensed under the MIT License. See the [LICENSE](LICENSE) file for details.