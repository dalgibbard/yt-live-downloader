FROM python:3-slim

RUN mkdir -p /app
WORKDIR /app
COPY *.py /app/
COPY requirements.txt /app/
COPY templates/ /app/templates/
RUN python3 -m pip install -r requirements.txt

EXPOSE 5000
CMD ["./app.py"]
