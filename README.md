How I got this running on Heroku:
- (I ignored everything outside the density directory (so scripts/Vagrantfile are all ignored)
- changed __init__.py to app.py (I didn’t have to do this, but I felt like it)
    - added “an app.run() to bottom of this file
- predict_tomorrow didn’t exist so renamed predict_today bc that worked
- made a procfile/requirements.txt (required for heroku/dokku)
- on heroku, added a postgres db
- followed this guide to access db and add needed tables https://stackoverflow.com/questions/20775490/how-to-create-or-manage-heroku-postgres-database-instance
    - after you login run the code in these two files (in this order):
        - https://github.com/ADI-Labs/density/blob/master/scripts/schema.sql
        - https://github.com/ADI-Labs/density/blob/master/scripts/dump.sql


Also:
- I removed the map link from nabber and iOS app link because neither work
- predict/library times doesn’t work
- Google CLIENT ID is currently public/should be changed




## Update
This site is also on Digital Ocean with mocked data to show it works there. See it live [here](http://www.cucdss.com/).

### Helpful stuff for Dokku deployment

exposing Dokku postgres https://stackoverflow.com/questions/34851335/accessing-postgres-database-inside-dokku-container-from-outside/35416785

https://www.digitalocean.com/community/questions/having-subdomain-of-namecheap-domain-point-to-droplet

env - http://dokku.viewdocs.io/dokku/configuration/environment-variables/

maybe https://github.com/githubsaturn/captainduckduck


https://www.digitalocean.com/community/tutorials/how-to-use-the-dokku-one-click-digitalocean-image-to-deploy-a-python-flask-app

load in a sql file:
dokku postgres:connect app < file.sql

dokku config:set app VARIABLE_NAME = "VALUE"
