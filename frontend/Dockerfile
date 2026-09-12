ARG DOCKER_REGISTRY=""
FROM ${DOCKER_REGISTRY}nginx:alpine

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY . /usr/share/nginx/html

EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]
