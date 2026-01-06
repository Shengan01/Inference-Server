# Grafana with Prometheus Auto-provisioning
FROM grafana/grafana:11.0.0

# Disable login for demo purposes
ENV GF_AUTH_ANONYMOUS_ENABLED=true
ENV GF_AUTH_ANONYMOUS_ORG_ROLE=Admin
ENV GF_AUTH_DISABLE_LOGIN_FORM=true
ENV GF_SECURITY_ALLOW_EMBEDDING=true

# Copy provisioning files
COPY provisioning /etc/grafana/provisioning
COPY dashboards /var/lib/grafana/dashboards

EXPOSE 3000
