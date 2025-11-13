from sc_assistant import create_app

# Create the Flask app using the factory pattern
app = create_app()

if __name__ == "__main__":
    # You can configure host and port here or in a Gunicorn config
    app.run(host="0.0.0.0", port=8080, debug=True)