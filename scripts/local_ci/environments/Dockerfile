ARG BASE_IMAGE
FROM ${BASE_IMAGE}
USER 0:0
COPY payload/ /opt/local-ci/runtime/
COPY image_prepare.py /opt/local-ci/image_prepare.py
COPY image-recipe.json /opt/local-ci/image-recipe.json
RUN python3 -I -S -B /opt/local-ci/image_prepare.py \
    && rm /opt/local-ci/image_prepare.py /opt/local-ci/image-recipe.json \
    && mkdir -p /opt/local-ci/control
WORKDIR /task
ENTRYPOINT ["/bin/sh", "-c", "trap 'exit 0' TERM INT; while :; do sleep 3600 & wait $!; done"]
