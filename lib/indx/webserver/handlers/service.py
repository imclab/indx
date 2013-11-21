import logging, json, subprocess,sys, os

from indx.webserver.handlers.base import BaseHandler
from indxclient import IndxClient

class ServiceHandler(BaseHandler):

    def __init__(self, server, service_path):
        BaseHandler.__init__(self, server, register=False)
        self.pipe = None
        self.service_path = service_path
        self.subhandlers = self._make_subhandlers()

    def is_service(self):
        manifest = self._load_manifest()
        return 'type' in manifest and 'service' in manifest['type']

    def on_boot(self):
        manifest = self._load_manifest()
        return 'on_boot' in manifest and manifest['on_boot']

    def _make_subhandlers(self):
        return [
            {
                "prefix": "{0}/api/set_config".format(self.service_path),
                'methods': ['GET'],
                'require_auth': True,
                'require_token': False,
                'handler': ServiceHandler.set_config,
                'accept':['application/json'],
                'content-type':'application/json'
            },
            {
                "prefix": "{0}/api/get_config".format(self.service_path),
                'methods': ['GET'],
                'require_auth': True,
                'require_token': False,
                'handler': ServiceHandler.get_config,
                'accept':['application/json'],
                'content-type':'application/json'
            },    
            {
                "prefix": "{0}/api/start".format(self.service_path),
                'methods': ['GET'],
                'require_auth': True,
                'require_token': False,
                'handler': ServiceHandler.start_handler,
                'accept':['application/json'],
                'content-type':'application/json'
            },
            {
                "prefix": "{0}/api/stop".format(self.service_path),
                'methods': ['GET'],
                'require_auth': True,
                'require_token': False,
                'handler': ServiceHandler.stop_handler,
                'accept':['application/json'],
                'content-type':'application/json'
            },
            {
                "prefix": "{0}/api/is_running".format(self.service_path),
                'methods': ['GET'],
                'require_auth': True,
                'require_token': False,
                'handler': ServiceHandler.is_running_handler,
                'accept':['application/json'],
                'content-type':'application/json'
            }
        ]

    def _load_manifest(self):
            # throw error!
        if self.service_path is None:
            logging.error("Error in _load_manifest: Cannot find manifest - no service path set!") 
            return self.return_internal_error("No service path set. Cannot find manifest")
        manifest_path = "apps/{0}/manifest.json".format(self.service_path)
        manifest_data = open(manifest_path)
        manifest = json.load(manifest_data)
        manifest_data.close()
        return manifest

    def get_config(self, request):
        try:
            manifest = self._load_manifest()
            result = subprocess.check_output(manifest['get_config'])
            logging.debug(' get config result {0} '.format(result))
            result = json.loads(result)
            logging.debug(' get json config result {0} '.format(result))
            return self.return_ok(request,data={'config':result})
        except :
            logging.error("Error in get_config {0}".format(sys.exc_info()[0]))
            return self.return_internal_error(request)
        
    def set_config(self, request): 
        try:
            # invoke external process to set their configs
            logging.debug("set_config -- getting config from request")        
            config = self.get_arg(request, "config")
            logging.debug("set_config config arg {0}".format(config))
            ## load the manifest 
            manifest = self._load_manifest()
            jsonconfig = json.dumps(config)
            # somewhere inside this we have put {0} wildcard so we wanna substitute that
            # with the actual config obj
            expanded = [x.format(jsonconfig) for x in manifest['set_config']]
            result = subprocess.call(expanded)
            logging.debug("result of subprocess call {0}".format(result))
            return self.return_ok(request, data={'result': result})
        except :
            logging.error("Error in set_config {0}".format(sys.exc_info()[0]))
            return self.return_internal_error(request)

    def is_running(self):
        if self.pipe is not None:
            logging.debug(" pipe poll {0}".format(self.pipe.poll()))
        return self.pipe is not None and self.pipe.poll() is None

    def start(self):
        if self.is_running():
           self.stop()
        manifest = self._load_manifest()
        command = [x.format(self.webserver.server_url) for x in manifest['run']]
        self.pipe = subprocess.Popen(command)
        return self.is_running()

    def start_handler(self,request):
        result = self.start()
        return self.return_ok(request, data={'result': result})

    def stop(self):
        if self.is_running():
            self.pipe.kill()
        self.pipe = None

    def stop_handler(self,request):
        self.stop()
        return self.return_ok(request)

    def is_running_handler(self,request):
        return self.return_ok(request, data={'running': self.is_running()})

    def render(self, request):
        logging.debug("SERVICE HANDLER RENDER :::::::::::::::::::::::::: ")
        return BaseHandler.render(self,request)

