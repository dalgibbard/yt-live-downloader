FROM python:3-slim
RUN python3 -m pip install Flask flask_basicauth requests

RUN mkdir -p /src
COPY *.py /src/
COPY templates/ /src/templates/

WORKDIR /src
EXPOSE 5555
CMD ["./app.py"]
