#    Copyright (C) 2011-2013 University of Southampton
#    Copyright (C) 2011-2013 Daniel Alexander Smith
#    Copyright (C) 2011-2013 Max Van Kleek
#    Copyright (C) 2011-2013 Nigel R. Shadbolt
#
#    This program is free software: you can redistribute it and/or modify
#    it under the terms of the GNU Affero General Public License, version 3,
#    as published by the Free Software Foundation.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU Affero General Public License for more details.
#
#    You should have received a copy of the GNU Affero General Public License
#    along with this program.  If not, see <http://www.gnu.org/licenses/>.

import logging
from indx.webserver.handlers.base import BaseHandler
import indx.indx_pg2 as database

class AuthHandler(BaseHandler):
    base_path = 'auth'
    # authentication
    def auth_login(self, request):
        """ User logged in (POST) """
        ## @TODO : check username/password by authenticating against postgres
        ## 1. check username/password
        ##     if auth -> save username/password in wbsession
        ##                return ok
        ##     else: -> return error

        try:
            args = self.get_post_args(request)
            logging.debug(" request args {0} ".format(args))            
            user = args['username'][0]
            pwd = args['password'][0]
        except Exception as e:
            logging.error("Error  {0}".format(e))
            return self.return_unauthorized(request)

        def win():
            logging.debug("Login request auth for {0}, origin: {1}".format(user, request.getHeader("Origin")))
            wbSession = self.get_session(request)
            wbSession.setAuthenticated(True)
            wbSession.setUser(user)
            wbSession.setPassword(pwd)
            self.return_ok(request)
        
        def fail():
            logging.debug("Login request fail, origin: {0}".format(self.get_origin(request)))
            wbSession = self.get_session(request)
            wbSession.setAuthenticated(False)
            wbSession.setUser(None)
            wbSession.setPassword(None)            
            self.return_unauthorized(request)

        database.auth(user,pwd).addCallback(lambda loggedin: win() if loggedin else fail())

    def auth_logout(self, request):
        """ User logged out (GET, POST) """
        logging.debug("Logout request, origin: {0}".format(self.get_origin(request)))
        wbSession = self.get_session(request)
        wbSession.setAuthenticated(False)
        wbSession.setUser(None)
        wbSession.setPassword(None)        
        self.return_ok(request)

    def auth_whoami(self, request):
        wbSession = self.get_session(request)
        logging.debug('auth whoami ' + repr(wbSession))
        self.return_ok(request, { "user" : wbSession and wbSession.username or 'nobody', "is_authenticated" : wbSession and wbSession.is_authenticated })
        
    def get_token(self,request):
        ## 1. request contains appid & box being requested (?!)
        ## 2. check session is Authenticated, get username/password
        try:
            args = self.get_post_args(request)
            logging.debug(" request args {0} ".format(args))            
            appid = args['app'][0]
            boxid = args['box'][0]
        except Exception as e:
            logging.error("Error parsing arguments: {0}".format(e))
            return self.return_bad_request(request)

        wbSession = self.get_session(request)
        if not wbSession.is_authenticated:
            return self.return_unauthorized(request)

        username = wbSession.username
        password = wbSession.password
        origin = self.get_origin(request)

        def check_app_perms(conn):
            token = self.webserver.tokens.new(username,password,boxid,appid,origin,request.getClientIP())
            return self.return_ok(request, {"token":token.id})

        # create a connection pool
        database.connect_box(boxid,username,password).addCallbacks(check_app_perms, lambda conn: self.return_forbidden(request))

AuthHandler.subhandlers = [
    {
        'prefix':'whoami',
        'methods': ['GET'],
        'require_auth': False,
        'require_token': False,
        'handler': AuthHandler.auth_whoami,
        'content-type':'text/plain', # optional
        'accept':['application/json']      
    },    
    {
        'prefix':'login',
        'methods': ['POST'],
        'require_auth': False,
        'require_token': False,
        'handler': AuthHandler.auth_login,
        'content-type':'text/plain', # optional
        'accept':['application/json']                
        },
    {
        'prefix':'logout',
        'methods': ['POST', 'GET'],
        'require_auth': True,
        'require_token': False,
        'handler': AuthHandler.auth_logout,
        'content-type':'text/plain', # optional
        'accept':['application/json']        
    },
    {
        'prefix':'get_token',
        'methods': ['POST'],
        'require_auth': True,
        'require_token': False,
        'handler': AuthHandler.get_token,
        'content-type':'application/json', 
        'accept':['application/json']                
    },
    {
        'prefix':'logout',
        'methods': ['POST', 'GET'],
        'require_auth': False,
        'require_token': False,
        'handler': AuthHandler.return_ok, # already logged out
        'content-type':'text/plain', # optional
        'accept':['application/json']                
        },
     {
        'prefix':'*',
        'methods': ['OPTIONS'],
        'require_auth': False,
        'require_token': False,
        'handler': AuthHandler.return_ok, # already logged out
        'content-type':'text/plain', # optional
        'accept':['application/json']                
     }    
]

