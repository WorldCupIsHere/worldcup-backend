from fastapi import FastAPI

app = FastAPI()

@app.get("/")
def root():
    return {"message": "World Cup API is working!"}
