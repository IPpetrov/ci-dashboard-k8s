from app import create_app
app = create_app()

@app.route("/health")
def health():
    return "OK", 200