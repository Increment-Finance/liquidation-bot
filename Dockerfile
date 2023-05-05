FROM python:3.10.9

WORKDIR /app

COPY . .

RUN pip install -r requirements.txt

CMD ["python", "Liquidation.py"]
