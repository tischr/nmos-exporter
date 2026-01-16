# Prometheus NMOS BCP-008 Exporter

An exporter that queries BCP-008 sender and receiver information of NMOS nodes using IS-12, and exposes them as Prometheus metrics. 

## Installation

Build and run the docker image. An example docker-compose.yml, including Prometheus can be found under /examples.

```bash
git clone git@github.com:tischr/nmos-exporter.git && cd ./nmos-exporter
docker build -t nmos-exporter .
cd ./examples
docker compose up -d 
```
To test against the [nmos-device-control-mock](https://github.com/AMWA-TV/nmos-device-control-mock), the exporter needs to run without network seperation in network_mode host (the mock device ws points to localhost). 

## Usage

The exporter queries the NMOS nodes at scrape time, using the target parameter (similar to the snmp-exporter). An example prometheus.yml can be found under /examples. 

```python
- job_name: "nmos-exporter"

    metrics_path: /probe

    static_configs:
    # Set IP addresses and port of NMOS nodes here
      - targets:
          - your-nmos-node:8080

    relabel_configs:
      # Pass the original target as the "target" query param
      - source_labels: [__address__]
        target_label: __param_target

      # Set instance label to the probed target
      - source_labels: [__param_target]
        target_label: instance

      # Set nmos-exporter address and port here
      - target_label: __address__
        replacement: ip-of-exporter:9080
```

## Contributing

Pull requests are welcome.

