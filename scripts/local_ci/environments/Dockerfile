ARG BASE_IMAGE
FROM ${BASE_IMAGE}
USER 0:0
COPY payload/ /opt/local-ci/runtime/
COPY control/ /opt/local-ci/control/
COPY image-recipe.json /opt/local-ci/image-recipe.json
RUN python3 -I -S -B /opt/local-ci/control/scripts/local_ci/environments/container_fs.py prepare-image
WORKDIR /task
ENTRYPOINT ["/bin/sh", "-c", "trap 'exit 0' TERM INT; while :; do sleep 3600 & wait $!; done"]
