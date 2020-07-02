from setuptools import setup

setup(
    name="bvp",
    description="Balancing Valorisation Platform.",
    author="Seita BV",
    author_email="nicolas@seita.nl",
    keywords=["smart grid", "renewables", "balancing", "forecasting", "scheduling"],
    version="0.2",
    # flask should be after all the flask plugins, because setup might find they ARE flask
    install_requires=[
        "bokeh==1.0.4",  # ui/utils/plotting_utils separate_legend() and create_hover_tool()
        "colour",
        "pscript",
        "pandas",
        "pandas-bokeh==0.4.3",  # 0.5 requires bokeh>=2.0, but bokeh still doesn't support sharing a legend across plots
        "iso8601",
        "xlrd",
        "inflection",
        "inflect",
        "humanize",
        "psycopg2-binary",
        "bcrypt",
        "pytz",
        "tzlocal",
        "numpy",
        "isodate",
        "click",
        "validate_email",
        "email_validator",  # required by WTForms (itself a Flask-Security dependency)
        "rq",
        "rq-dashboard",
        "rq-win; os_name=='nt'",
        "redis; os_name=='nt'",
        "py3DNS",
        "pyomo>=5.6",
        "forecastiopy",
        "pysolar",
        "timetomodel>=0.6.8",
        "python-dotenv",
        "Flask-SSLify",
        "Flask_JSON",
        "Flask-SQLAlchemy>=2.4.3",
        "Flask-Migrate",
        "Flask-Classful",
        "Flask-WTF",
        "Flask-Login==0.4.1",  # Todo: remove once https://github.com/mattupstate/flask-security/issues/856 is solved
        "Flask-Mail",
        "Flask-Security",
        "Flask-Marshmallow",
        "marshmallow-sqlalchemy>=0.23.1",
        "flask>=1.0",
    ],
    setup_requires=["pytest-runner"],
    tests_require=[
        "pytest",
        "pytest-flask",
        "pytest-cov",
        "flake8-bugbear",  # nicer error messages
        "requests",  # to test calls to the API
        "fakeredis",  # let's tests run successfully in containers
        "lupa",  # required with fakeredis, maybe because we use rq
    ],
    packages=["bvp"],
    include_package_data=True,
    # license="Apache",
    classifiers=[
        "Programming Language :: Python",
        "Programming Language :: Python :: 3",
        "Development Status :: 3 - Alpha",
        "License :: OSI Approved :: GNU Library or Lesser General Public License (LGPL)",
        "Operating System :: OS Independent",
    ],
    long_description="""\
The *Balancing Valorisation Platform (BVP)* is a tool for scheduling balancing actions on behalf of the connected
asset owners. Its purpose is to offer these balancing actions as one aggregated service to energy markets,
realising the highest possible value for its users.
""",
)
