from flask import Flask
from flask_caching import Cache
from prometheus_flask_exporter import PrometheusMetrics

cache = Cache()

def create_app():
    app = Flask(__name__)

    app.config['CACHE_TYPE'] = 'SimpleCache'
    app.config['CACHE_DEFAULT_TIMEOUT'] = 300 
    cache.init_app(app)
    app.cache = cache

    metrics = PrometheusMetrics(app)
    metrics.info('ci_dashboard_info', 'CI Dashboard application info', version='1.0.0')


    from .routes import main
    app.register_blueprint(main)

    return app
