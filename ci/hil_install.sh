#!/usr/bin/env bash

# Setup configuration
cp ci/testsuite.cfg.$DB testsuite.cfg
chmod 0600 testsuite.cfg
sudo cp ci/apache/hil.cfg.$DB /etc/hil.cfg
sudo chown travis:travis /etc/hil.cfg
sudo chmod 0600 /etc/hil.cfg

# Database Setup
if [ $DB = postgres ]; then
    sudo apt-get install -y python-psycopg2
    psql --version
    psql -c 'CREATE DATABASE hil_tests;' -U postgres
    psql -c 'CREATE DATABASE hil;' -U postgres
fi

# Address #577 via
# https://stackoverflow.com/questions/2192323/what-is-the-python-egg-cache-python-egg-cache
mkdir -p ~/.python-eggs
chmod go-w ~/.python-eggs # Eliminate "writable by group/others" warnings

# Install HIL, incl. test dependencies
pip install .[tests]
