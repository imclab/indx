#    This file is part of WebBox.
#
#    Copyright 2011-2012 Daniel Alexander Smith
#    Copyright 2011-2012 University of Southampton
#
#    WebBox is free software: you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation, either version 3 of the License, or
#    (at your option) any later version.
#
#    WebBox is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#
#    You should have received a copy of the GNU General Public License
#    along with WebBox.  If not, see <http://www.gnu.org/licenses/>.


import logging, re, urllib2, uuid, rdflib, os, os.path, traceback, mimetypes, time, shutil

from cStringIO import StringIO

from twisted.web import script
from twisted.web.static import File, Registry
from twisted.web.wsgi import WSGIResource
from twisted.internet import reactor
from lxml import objectify

from rdflib.graph import Graph
from time import strftime
from urlparse import urlparse
from webboxhandler import WebBoxHandler
from subscriptions import Subscriptions
from journal import Journal
from websocketclient import WebSocketClient
from mimeparse import best_match
from httputils import resolve_uri, http_get, http_put, http_post
from sparqlresults import SparqlResults
from sparqlparse import SparqlParse
from fourstoremgmt import FourStoreMgmt
from fourstore import FourStore
from exception import ResponseOverride
from wsupdateserver import WSUpdateServer

from urlparse import urlparse, parse_qs
from rdflib.serializer import Serializer
from rdflib.plugin import register
import rdfliblocal.jsonld

class WebBox:
    # to use like WebBox.to_predicate
    webbox_ns = "http://webbox.ecs.soton.ac.uk/ns#"

    to_predicate = "http://rdfs.org/sioc/ns#addressed_to"
    address_predicate = webbox_ns + "address"
    files_graph = webbox_ns + "UploadedFiles" # the graph with metadata about files (non-RDF)
    subscribe_predicate = webbox_ns + "subscribe_to" # uri for subscribing to a resource
    unsubscribe_predicate = webbox_ns + "unsubscribe_from" # uri for unsubscribing from a resource
    received_graph = webbox_ns + "ReceivedGraph" # default URI for 4store inbox

    def __init__(self, config):

        # use 4store query_store
        self.query_store = FourStore(config['4store']['host'], config['4store']['port'])

        self.config = config # configuration from server

        self.server_url = config["url"] # base url of the server, e.g. http://localhost:8212
        self.file_dir = os.path.join(config['webbox_dir'],config['file_dir'])
        self.file_dir = os.path.realpath(self.file_dir) # where to store PUT files

        logging.debug("Started new WebBox at URL: " + self.server_url)


        # config rdflib first
        register("json-ld", Serializer, "rdfliblocal.jsonld", "JsonLDSerializer")

        # mime type to rdflib formats (for serializing)
        self.rdf_formats = {
            "application/rdf+xml": "xml",
            "application/n3": "n3",
            "text/turtle": "n3", # no turtle-specific parser in rdflib ATM, using N3 one because N3 is a superset of turtle
            "text/plain": "nt",
            "application/json": "json-ld",
            "text/json": "json-ld",
        }


        # start websockets server
        self.wsupdate = WSUpdateServer(port=config['ws_port'], host=config['ws_hostname'])

        self.websocket = WebSocketClient(host=config['ws_hostname'],port=config['ws_port'])

        # run 4store 
        if 'delay' in config['4store']:
            delay = config['4store']['delay'] # delay (in seconds) between running the backend and https
        else:
            delay = 0
        self.fourstore = FourStoreMgmt(config['4store']['kbname'], http_port=config['4store']['port'], delay=delay) 
        self.fourstore.start()

        # set up the twisted web resource object

        # Disable directory listings
        class FileNoDirectoryListings(File):
            def directoryListing(self):
                return ForbiddenResource()

        # allow the config to be readable by .rpy files
        self.registry = Registry()
        self.registry.setComponent(WebBox, self)

        # root handler is a static web server
        self.resource = FileNoDirectoryListings(os.path.abspath(config["html_dir"]), registry=self.registry)
        self.resource.processors = {'.rpy': script.ResourceScript}
        self.resource.ignoreExt('.rpy')

        # add the webbox handler as a subdir
        self.resource.putChild("webbox", WSGIResource(reactor, reactor.getThreadPool(), self.response))

    def get_resource(self):
        """ Get the twisted web resource. """
        return self.resource


    def stop(self):
        """ Shut down the web box. """
        self.fourstore.stop()

    def response(self, environ, start_response):
        """ WSGI response handler."""

        logging.debug("Calling WebBox response(): " + str(environ))
        try:
            req_type = environ['REQUEST_METHOD']
            rfile = environ['wsgi.input']

            if "REQUEST_URI" in environ:
                url = urlparse(environ['REQUEST_URI'])
                req_path = url.path
                req_qs = parse_qs(url.query)
            else:
                req_path = environ['PATH_INFO']
                req_qs = parse_qs(environ['QUERY_STRING'])

            if req_type == "POST":
                response = self.do_POST(rfile, environ, req_path, req_qs)
            elif req_type == "PUT":
                response = self.do_PUT(rfile, environ, req_path, req_qs)
            elif req_type == "GET":
                response = self.do_GET(environ, req_path, req_qs)
            elif req_type == "OPTIONS":
                response = self.do_OPTIONS()
            elif req_type == "PROPFIND":
                response = self.do_PROPFIND(rfile, environ, req_path, req_qs)
            elif req_type == "LOCK":
                response = self.do_LOCK(rfile, environ, req_path, req_qs)
            elif req_type == "UNLOCK":
                response = self.do_UNLOCK(rfile, environ, req_path, req_qs)
            elif req_type == "DELETE":
                response = self.do_DELETE(rfile, environ, req_path, req_qs)
            elif req_type == "MKCOL":
                response = self.do_MKCOL(rfile, environ, req_path, req_qs)
            elif req_type == "MOVE":
                response = self.do_MOVE(rfile, environ, req_path, req_qs)
            elif req_type == "COPY":
                response = self.do_COPY(rfile, environ, req_path, req_qs)
            elif req_type == "HEAD":
                response = self.do_GET(environ, req_path, req_qs)
            else:
                response = {"status": 405, "reason": "Method Not Allowed", "data": ""}

            # get headers from response if they exist
            headers = []
            if "headers" in response:
                headers = response['headers']

            # set a content-type
            if "type" in response:
                headers.append( ("Content-type", response['type']) )
            else:
                headers.append( ("Content-type", "text/plain") )

            headers.append( ("DAV", "1, 2") )

            # add CORS headers (blanket allow, for now)
            headers.append( ("Access-Control-Allow-Origin", "*") )
            headers.append( ("Access-Control-Allow-Methods", "POST, GET, PUT, HEAD, OPTIONS") )

            # put repository version weak ETag header
            # journal to load the original repository version

            # NOTE have to re-load journal here (instead of using self.journal) because different threads can't share the same sqlite object
            j = Journal(os.path.join(self.config['webbox_dir'], self.config['journal']))
            
            latest_hash = j.get_version_hashes() # current and previous
            
            if latest_hash['current'] is not None:
                headers.append( ('ETag', "W/\"%s\""%latest_hash['current'] ) ) # 'W/' means a Weak ETag
            if latest_hash['previous'] is not None:
                headers.append( ('X-ETag-Previous', "W/\"%s\""%latest_hash['previous']) ) # 'W/' means a Weak ETag

            if "size" in response:
                data_length = response['size']
            else:
                data_length = len(response['data'])
            
            headers.append( ("Content-length", data_length) )

            start_response(str(response['status']) + " " + response['reason'], headers)
            logging.debug("Sending data of size: "+str(data_length))
           
            res_type = type(response['data'])
            logging.debug("Response type is: "+str(res_type))

            if req_type == "HEAD": # GET was called, so let's not return the body
                return []


            if res_type is unicode:
                response['data'] = response['data'].encode('utf8')

            if res_type is str or res_type is unicode:
                logging.debug("Returning a string")
                return [response['data']]
            else:
                logging.debug("Returning an iter")
                return response['data']

        except ResponseOverride as e:
            response = e.get_response()
            logging.debug("Response override raised, sending: %s" % str(response))
            start_response(str(response['status']) + " " + response['reason'], [])
            return [response['data']]

        except Exception as e:
            logging.debug("Error in WebBox.response(), returning 500: %s, exception is: %s" % (str(e), traceback.format_exc()))
            start_response("500 Internal Server Error", ())
            return [""]

    def get_prop_xml(self, url, path, directory=False, displayname=None):
        """ Get the property XML (for WebDAV) for this file. """

        stat = os.stat(path)
        creation = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.gmtime(stat.st_ctime) )
        length = str(stat.st_size)
        modified = time.strftime("%A, %d-%b-%y %H:%M:%S GMT", time.gmtime(stat.st_mtime) )

        if directory or os.path.isdir(path):
            resourcetype = "<D:resourcetype><D:collection/></D:resourcetype>"
        else:
            resourcetype = "<D:resourcetype/>"

        if displayname is not None:
            displayname = "<D:displayname>%s</D:displayname>" % displayname
        else:
            displayname = ""

        return """
<D:response>
 <D:href>%s</D:href>
 <D:propstat>
  <D:prop>
   <D:quota-available-bytes/>
   <D:quota-used-bytes/>
   <D:quota/>
   <D:quotaused/>
   <D:creationdate>
    %s
   </D:creationdate>
   <D:getcontentlength>
    %s
   </D:getcontentlength>
   <D:getlastmodified>
    %s
   </D:getlastmodified>
   %s
   %s
   <D:supportedlock>
    <D:lockentry>
     <D:lockscope><D:exclusive/></D:lockscope>
     <D:locktype><D:write/></D:locktype>
    </D:lockentry>
    <D:lockentry>
     <D:lockscope><D:shared/></D:lockscope>
     <D:locktype><D:write/></D:locktype>
    </D:lockentry>
   </D:supportedlock>
  </D:prop>
 </D:propstat>
</D:response>
""" % (url, creation, length, modified, displayname, resourcetype)

    def do_DELETE(self, rfile, environ, req_path, req_qs):
        """ Delete a file / dir, used by WebDAV. """

        file_path = self.get_file_path(req_path)
        logging.debug("Deleting %s" % file_path)

        if not os.path.exists(file_path):
            return{"status": 404, "reason": "Not Found", "data": ""}

        if os.path.isdir(file_path):
            os.rmdir(file_path)
        else:
            os.remove(file_path)

        return {"status": 204, "reason": "No Content", "data": ""}

    def do_MKCOL(self, rfile, environ, req_path, req_qs):
        """ WebDAV command to make a collection (i.e., a directory). """

        file_path = self.get_file_path(req_path)
        logging.debug("Making new folder %s" % file_path)
        if os.path.exists(file_path):
            raise ResponseOverride(409, "Conflict")

        try:
            os.mkdir(file_path)
        except Exception as e:
            logging.error("Couldn't make directory: " + str(file_path))
            raise ResponseOverride(500, "Internal Server Error")

        return {"status": 204, "reason": "No Content", "data": ""}

    def strip_server_url(self, url):
        """ Return the path with the webbox server URL stripped off. """

        wb_url = urlparse(self.server_url)
        fixed_path = wb_url.path # e.g. /webbox
        
        new_url = urlparse(url)
        if not new_url.path.startswith(fixed_path):
            logging.error("Requested URL (%s) does not start with our webbox path (%s)" % (url, fixed_path))
            raise ResponseOverride(500, "Internal Server Error")

        # strip off fixed_path
        return new_url.path[len(fixed_path):]


    def do_MOVE(self, rfile, environ, req_path, req_qs):
        """ WebDAV command to move (rename) a file. """

        file_path = self.get_file_path(req_path)
        logging.debug("Moving from file %s" % file_path)
        if not os.path.exists(file_path):
            raise ResponseOverride(404, "Not Found")

        dest_file_path = self.get_file_path(self.strip_server_url(environ['HTTP_DESTINATION']))
        logging.debug("Moving to file %s" % dest_file_path)

        os.rename(file_path, dest_file_path)

        return {"status": 204, "reason": "No Content", "data": ""}

    def do_COPY(self, rfile, environ, req_path, req_qs):
        """ WebDAV command to copy a file. """

        file_path = self.get_file_path(req_path)
        logging.debug("Copying from file %s" % file_path)
        if not os.path.exists(file_path):
            raise ResponseOverride(404, "Not Found")

        dest_file_path = self.get_file_path(self.strip_server_url(environ['HTTP_DESTINATION']))
        logging.debug("Copying to file %s" % dest_file_path)

        if os.path.isdir(file_path):
            shutil.copytree(file_path, dest_file_path)
        else:
            shutil.copyfile(file_path, dest_file_path)

        return {"status": 204, "reason": "No Content", "data": ""}


    def do_PROPFIND(self, rfile, environ, req_path, req_qs):
        logging.debug("WebDAV PROPFIND")

        size = 0
        if environ.has_key("CONTENT_LENGTH"):
            size = int(environ['CONTENT_LENGTH'])

        file = ""
        if size > 0:
            # read into file
            file = rfile.read(size)

        if file != "":
            logging.debug("got request: " + file)

        # FIXME we ignore the specifics of the request and just give what Finder wants: getlastmodified, getcontentlength, creationdate and resourcetype
        xmlout = ""

        file_path = self.get_file_path(req_path)
        logging.debug("For PROPFIND file is: "+file_path)

        if os.path.exists(file_path):
            if os.path.isdir(file_path):
                # do an LS
                displayname = None
                if req_path == "/":
                    displayname = "WebBox"
                xmlout += self.get_prop_xml(self.server_url + req_path, file_path, directory=True, displayname=displayname)
                for filename in os.listdir(file_path):
                    fname = filename
                    if req_path[-1:] != "/":
                        fname = "/" + filename

                    xmlout += self.get_prop_xml(self.server_url + req_path + filename, file_path + os.sep + filename)
            else:
                # return the properties for a single file
                xmlout += self.get_prop_xml(self.server_url + req_path, file_path)
        else:
            logging.debug("Not found for PROPFIND")
            return {"status": 404, "reason": "Not Found", "data": ""}

        # surround in xml
        xmlout = "<?xml version=\"1.0\" encoding=\"utf-8\" ?>\n<D:multistatus xmlns:D=\"DAV:\">" + xmlout + "\n</D:multistatus>"

        xmlout = xmlout.encode("utf8")

        logging.debug("Sending propfind: " + xmlout)

        return {"status": 207, "reason": "Multi-Status", "data": xmlout, "type": "text/xml; charset=\"utf-8\""}

        
    def do_LOCK(self, rfile, environ, req_path, req_qs):
        logging.debug("WebDAV Lock on file: "+req_path)

        try:
            size = 0
            if environ.has_key("CONTENT_LENGTH"):
                size = int(environ['CONTENT_LENGTH'])

            file = ""
            if size > 0:
                # read into file
                file = rfile.read(size)

            fileroot = self.server_url + req_path

            x = objectify.fromstring(file)

            owner = x.owner.href.text
            lockscope = str([ el.tag for el in x.lockscope.iterchildren() ][0])[6:] # 6: gets rid of {DAV:}
            locktype = str([ el.tag for el in x.locktype.iterchildren() ][0])[6:]
            

            token = "urn:uuid:%s" % (str(uuid.uuid1()))

            lock = """
           <D:locktype><D:%s/></D:locktype>
           <D:lockscope><D:%s/></D:lockscope>
           <D:depth>infinity</D:depth>
           <D:owner>
             <D:href>%s</D:href>
           </D:owner>
           <D:timeout>Second-604800</D:timeout>
           <D:locktoken>
             <D:href
             >%s</D:href>
           </D:locktoken>
           <D:lockroot>
             <D:href
             >%s</D:href>
           </D:lockroot>
""" % (locktype, lockscope, owner, token, fileroot)

            lock = """
<?xml version="1.0" encoding="utf-8" ?> 
  <D:prop xmlns:D="DAV:"> 
    <D:lockdiscovery> 
      <D:activelock>
""" + lock + """
      </D:activelock>
    </D:lockdiscovery>
  </D:prop>
"""
            return {"status": 200, "reason": "OK", "data": lock, type: "application/xml; charset=\"utf-8\"", "headers": [("Lock-Token", "<"+token+">")]}

        except Exception as e:
            return {"status": 400, "reason": "Bad Request", "data": ""}



    def do_UNLOCK(self, rfile, environ, req_path, req_qs):
        logging.debug("WebDAV Unlock on file: "+req_path)
        # FIXME always succeeds
        return {"status": 204, "reason": "No Content", "data": ""}



    def do_OPTIONS(self):
        logging.debug("Sending 200 response to OPTIONS")
        return {"status": 200, "reason": "OK", "data": "", "headers":
            [ ("Allow", "PUT"),
              ("Allow", "GET"),
              ("Allow", "POST"),
              ("Allow", "HEAD"),
              ("Allow", "OPTIONS"),

              # WebDAV methods
              ("Allow", "PROPFIND"),
              ("Allow", "PROPPATCH"),
              #("Allow", "TRACE"),
              #("Allow", "ORDERPATCH"),
              ("Allow", "MKCOL"),
              ("Allow", "DELETE"),
              ("Allow", "COPY"),
              ("Allow", "MOVE"),
              ("Allow", "LOCK"),
              ("Allow", "UNLOCK"),
            ]
        } 

    def add_to_journal(self, graphuri):
        """ This Graph URI was added or changed, add to the journal. """

        logging.debug("Journal updating on graph: "+graphuri)

        repository_hash = uuid.uuid1().hex # TODO in future, make this a hash instead of a uuid

        journal = Journal(os.path.join(self.config['webbox_dir'], self.config['journal']))
        journal.add(repository_hash, [graphuri])

        self.update_websocket_clients()

    def update_websocket_clients(self):
        """ There has been an update to the webbox store, so send the changes to the clients connected via websocket. """
        logging.debug("Updating websocket clients...")

        journal = Journal(os.path.join(self.config['webbox_dir'], self.config['journal']))
        hashes = journal.get_version_hashes()
        if "previous" in hashes:
            previous = hashes["previous"]
            
            uris_changed = journal.since(previous)
            logging.debug("URIs changed: %s" % str(uris_changed))

            if len(uris_changed) > 0:

                ntrips = ""
                for uri in uris_changed:
                    query = "CONSTRUCT {?s ?p ?o} WHERE { GRAPH <%s> {?s ?p ?o}}" % uri
                    logging.debug("Sending query for triples as: %s " % query)

                    result = self.query_store.query(query, {"Accept": "text/plain"})
                    # graceful fail per U

                    rdf = result['data']
                    ntrips += rdf + "\n"

                self.websocket.sendMessage(ntrips, False)


    def get_subscriptions(self):
        """ Get a new subscriptions object, used by this class and also the webbox handler. """
        filename = os.path.join(self.config['webbox_dir'],self.config['subscriptions'])
        return Subscriptions(filename)

    def updated_resource(self, uri, type):
        """ Handle an update to a resource and send out updates to subcribers. """
        # type is "rdf" or "file"

        logging.debug("resource [%s] updated, notify subscribers" % uri)

        subscriptions = self.get_subscriptions()
        subscribers = subscriptions.get_subscribers(uri)
        logging.debug("subscribers are: %s" % str(subscribers))

        for subscriber in subscribers:
            try:
                status = self.send_message(subscriber, uri)
                if status is True:
                    logging.debug("notified %s about %s" % (subscriber, uri))
                else:
                    logging.debug("could not notify %s about %s: error was: %s" % (subscriber, uri, status))
            except Exception as e:
                logging.debug("error notifying subscriber: %s, moving on." % subscriber)

        return None # success

    def do_POST(self, rfile, environ, req_path, req_qs):
        """ Handle a POST (update). """
        # POST of RDF is a merge.

        post_uri = self.server_url + req_path
        logging.debug("POST of uri: %s" % post_uri)

        if environ.has_key("CONTENT_TYPE"):
            content_type = environ['CONTENT_TYPE']

        # if a .rdf is uploaded, set the content-type manually
        if req_path[-4:] == ".rdf":
            content_type = "application/rdf+xml"
        elif req_path[-3:] == ".n3":
            content_type = "text/turtle"
        elif req_path[-3:] == ".nt":
            content_type = "text/plain"

        # determine if this is a hidden file
        path_parts = req_path.split("/")
        hidden_file = False
        if len(path_parts) > 0 and len( path_parts[ len(path_parts) - 1 ]) > 0 and path_parts[ len(path_parts) - 1 ][0] == ".":
            hidden_file = True

        if content_type in self.rdf_formats and not hidden_file:
            logging.debug("content type of PUT is RDF so we also send to 4store.")

            rdf_format = self.rdf_formats[content_type]

            size = 0
            if environ.has_key("CONTENT_LENGTH"):
                size = int(environ['CONTENT_LENGTH'])

            # read RDF content into file
            file = ""
            if size > 0:
                file = rfile.read(size)
          
            # strip off the last slash if it is to /webbox/
            if post_uri == self.server_url + "/":
                post_uri = post_uri[:-1]

            # set the graph to PUT to. the URI itself, or ?graph= if set (compatibility with SPARQL1.1)
            graph = post_uri
            if req_qs.has_key('graph'):
                graph = req_qs['graph'][0]

            # if they have put to /webbox then we handle it any messages (this is the only URI that we handle messages on)
            if graph == self.server_url:
                graph = self.received_graph # save (append) into the received graph, not into the server url

                # deserialise rdf into triples so we can use them in python
                rdfgraph = Graph()
                rdfgraph.parse(data=file, format=rdf_format) # format = xml, n3 etc

                # check for webbox specific messages using the handler class
                handle_response = self._handle_webbox_rdf(rdfgraph) # check if rdf contains webbox-specific rdf, i.e. to_address, subscribe etc
                if handle_response is not None:
                    # TODO replace this with raising a ResponseOverride in the handler
                    return handle_response # i.e., there is an error, so return it


            # do SPARQL PUT
            logging.debug("WebBox SPARQL POST to graph (%s)" % (graph) )
            response = self.SPARQLPost(graph, file, content_type)
            if response['status'] > 299:
                # Return the store error if there is one.
                return {"data": "", "status": response['status'], "reason": response['reason']}


            self.updated_resource(post_uri, "rdf") # notify subscribers
            self.add_to_journal(graph) # update journal

            # TODO remake the file on disk (assuming graph is relative to our file) according to the new 4store status
            if not req_qs.has_key('graph'):
                # only make a file if it is a local URI
                file_path = self.get_file_path(req_path)
                logging.debug("file path is: %s" % file_path)
                exists = os.path.exists(file_path)

                # replace the file with RDF/XML (the default format for resolving URIs), so convert if we have to
                query = "CONSTRUCT {?s ?p ?o} WHERE { GRAPH <%s> {?s ?p ?o}}" % graph
                result = self.query_store.query(query, {"Accept": "application/rdf+xml"})
                rdf = result['data']

                # write the RDF/XML to the file
                f = open(file_path, "w")
                f.write(rdf)
                f.close()

                if exists:
                    return {"data": "", "status": 204, "reason": "No Content"}
                else:
                    return {"data": "", "status": 201, "reason": "Created"}
                    

            # Return 204
            return {"data": "", "status": 204, "reason": "No Content"}

        else:
            
            logging.debug("a POST to an existing non-rdf static file (non-sensical): sending a not allowed response")

            # When you send a 405 Not Allowed you have to send Allow headers saying which methods ARE allowed.
            headers = [ 
              ("Allow", "PUT"),
              ("Allow", "GET"),
              #invalid for a file#("Allow", "POST"),
              ("Allow", "HEAD"),
              ("Allow", "OPTIONS"),

              # WebDAV methods
              ("Allow", "PROPFIND"),
              ("Allow", "PROPPATCH"),
              #not impl#("Allow", "TRACE"),
              #not impl#("Allow", "ORDERPATCH"),
              #invalid for a file#("Allow", "MKCOL"),
              ("Allow", "DELETE"),
              ("Allow", "COPY"),
              ("Allow", "MOVE"),
              ("Allow", "LOCK"),
              ("Allow", "UNLOCK"),
            ]

            return {"data": "", "status": 405, "reason": "Not Allowed", "headers": headers}


    def handle_update(self, since_repository_hash):
        """ Handle calls to the Journal update URL. """

        journal = Journal(os.path.join(self.config['webbox_dir'], self.config['journal']))
        uris_changed = journal.since(since_repository_hash)
        logging.debug("URIs changed: %s" % str(uris_changed))

        if len(uris_changed) > 0:

            ntrips = ""
            for uri in uris_changed:
                query = "CONSTRUCT {?s ?p ?o} WHERE { GRAPH <%s> {?s ?p ?o}}" % uri
                logging.debug("Sending query for triples as: %s " % query)

                result = self.query_store.query(query, {"Accept": "text/plain"})
                # graceful fail per U

                rdf = result['data']
                ntrips += rdf + "\n"

            # TODO conneg
            rdf_type = "text/plain" # text/plain is n-triples (really)

            return {"data": ntrips, "status": 200, "reason": "OK", "type": rdf_type}

        else:
            # no update
            logging.debug("Client is up to date")
            return {"data": "", "status": 204, "reason": "No Content"}



    def do_GET(self, environ, req_path, req_qs):
        logging.debug("req_path: %s" % (req_path))

        # journal update called
        if req_path == "/update":
            since = None
            if "since" in req_qs:
                since = req_qs['since'][0]
            return self.handle_update(since)

        if req_qs.has_key("query"):
            # SPARQL query because ?query= is present
            query = req_qs['query'][0]
            response = self.query_store.query(query)

            sp = SparqlParse(query)
            verb = sp.get_verb()

            if verb == "SELECT":
                # convert back from a data structure
                sr = SparqlResults()
                results_xml = sr.sparql_results_to_xml(response['data'])
                response['data'] = results_xml
                response['type'] = "application/sparql-results+xml"
            elif verb == "CONSTRUCT":
                # rdf, so allow conversion of type
                # convert based on headers
                response = self._convert_response(response, environ)

            return response
        else:
            # is this a plain file that exists?

            file_path = self.get_file_path(req_path)

            if os.path.exists(file_path):
                # return the file
                logging.debug("Opening file: "+file_path)
                f = open(file_path, "r")
                size = os.path.getsize(file_path)
                #filedata = f.read()
                #f.close()

                if "HTTP_RANGE" in environ:
                    logging.debug("Byte range requested, returning as a string")
                    return {"data": self.get_byte_range(f, environ['HTTP_RANGE']), "status": 200, "reason": "OK"}
                else:
                    logging.debug("File read into file object started.")
                    return {"data": f, "status": 200, "reason": "OK", "size": size}
            else:
                return {"data": "", "status": 404, "reason": "Not Found"}

    def get_byte_range(self, file, byterange):
        """ Return a range of bytes as specified by the HTTP_RANGE header. """

        # byterange is like: bytes=1380533830-1380533837
        (offset, end) = byterange.split("bytes=")[1].split("-")
        length = int(end) - int(offset)

        file.seek(int(offset))
        data = file.read(length)
        file.close()
        return data

    def _strip_charset(self, mime):
        """ Strip the charset from a mime-type. """

        if ";" in mime:
            return mime[:mime.index(";")]

        return mime

    def _convert(self, data, from_type, to_type):
        """ Convert rdf from one mime-type to another. """
        from_type = self._strip_charset(from_type)
        to_type = self._strip_charset(to_type)

        old_format = self.rdf_formats[from_type]
        new_format = self.rdf_formats[to_type]

        graph = Graph()
        graph.parse(data=data, format=old_format) # format = xml, n3 etc

        # reserialise into new format
        new_data = graph.serialize(format=new_format) # format = xml, n3, nt, turtle etc
        return new_data

    def _convert_response(self, response, environ):
        logging.debug("convert_response, response: "+str(response))

        if not (response['status'] == 200 or response['status'] == "200 OK"):
            logging.debug("Not converting data, status is not 200.")
            return response # only convert 200 OK responses

        status = response['status']
        reason = response['reason']
        type = response['type'].lower()
        data = response['data']

        accept = environ['HTTP_ACCEPT'].lower()

        if accept == "*/*":
            logging.debug("Not converting, client accepts anything.") # for performance and compatiblity
            return response

        if accept == type:
            logging.debug("Not converting data, type is identical to that requested.")
            return response

        # data
        new_mime = best_match(self.rdf_formats.keys(), accept)
        if new_mime in self.rdf_formats:
            new_data = self._convert(data, type, new_mime)
            new_response = {"status": status,
                            "reason": reason,
                            "type": new_mime,
                            "data": new_data}

            logging.debug("Returning converted response from "+type+" to "+accept+".")
            return new_response

        else:
            logging.debug("Can't understand Accept header of "+accept+", so returning data as-is.")
            return response
        

    def _get_webbox(self, person_uri):

        query = "SELECT DISTINCT ?webbox WHERE { <%s> <%s> ?webbox } " % (person_uri, WebBox.address_predicate)
        response = self.query_store.query(query)
        if response['status'] >= 200 and response['status'] <= 299:
            results = response['data']
            for row in results:
                webbox = row['webbox']['value']
                logging.debug("found webbox uri: "+webbox)
                return webbox
        else:
            logging.error("Couldn't get webbox of person with uri %s, response: %s" % (person_uri, str(response)))
        
        return None

    def get_webbox(self, person_uri):
        """ Get the webbox URL of a person's URI. """

        logging.debug("looking up webbox URI of person: "+person_uri)
        response = self._get_webbox(person_uri)

        if response is not None:
            return response
        
        # did not have it in the local store, resolve it instead:
        try:
            rdf = resolve_uri(person_uri)
        except Exception as e:
            logging.debug("Did not resolve webbox from the person's (FOAF) URI: "+person_uri)
            return None

        logging.debug("resolved it.")

        # put into 4store 
        # put resolved URI into the store
        # put into its own graph URI in 4store
        response1 = self.SPARQLPut(person_uri, rdf, "application/rdf+xml")
        logging.debug("Put it in the store: "+str(status))

        if response1['status'] > 299:
            logging.debug("! error putting person uri into local store. status is: %s " % str(status))
            return None

        # TODO notify apps etc.
        logging.debug("Received message of URI, and put it in store: " + person_uri)


        logging.debug("looking up webbox URI of person: "+person_uri)
        response = self._get_webbox(person_uri)

        if response is not None:
            return response


        logging.debug("did not find webbox uri.")
        return None


    def send_message(self, recipient_uri, message_resource_uri):
        """ Send an external message to a recipient. """

        # URI to HTTP POST to
        webbox_uri = self.get_webbox(recipient_uri)
        if webbox_uri is None:
            logging.debug("Could not get webbox of " + recipient_uri)
            return "Couldn't get webbox of: " + recipient_uri

        # generate our RDF message to "POST"
        graph = Graph()
        graph.add(
            (rdflib.URIRef(message_resource_uri),
             rdflib.URIRef(WebBox.to_predicate),
             rdflib.URIRef(recipient_uri)))
        
        rdf = graph.serialize(format="xml") # rdf/xml

        req_uri = webbox_uri
        try:
            # HTTP POST to their webbox
            opener = urllib2.build_opener(urllib2.HTTPHandler)
            request = urllib2.Request(req_uri, data=rdf)
            request.add_header('Content-Type', 'application/rdf+xml') # because format="xml" above
            request.get_method = lambda: 'POST'
            url = opener.open(request)
            return True
        except Exception as e:
            """ Couldn't send a message fast-fail. """
            logging.debug("Couldn't send a message to: " + req_uri)
            return "Couldn't connect to %s" % req_uri

    def add_new_file(self, filename):
        """ Add a new file to the files graph (it was just updated/uploaded). """
        logging.debug("Adding a new file metadata to the store for file: "+filename)

        uri = self.server_url + os.sep + filename

        # create the RDF
        graph = None
        while graph is None:
            try:
                graph = Graph()
            except Exception as e:
                logging.debug("Got error making graph, trying again.")

        graph.add(
            (rdflib.URIRef(uri),
             rdflib.URIRef("http://www.w3.org/1999/02/22-rdf-syntax-ns#type"),
             rdflib.URIRef(self.webbox_ns + "File")))

        graph.add(
            (rdflib.URIRef(uri),
             rdflib.URIRef(self.webbox_ns+"filename"),
             rdflib.Literal(filename)))

        mimetype = mimetypes.guess_type(filename)[0]

        if mimetype is not None:
            graph.add(
                (rdflib.URIRef(uri),
                 rdflib.URIRef("http://www.semanticdesktop.org/ontologies/nie/#mimeType"),
                 rdflib.Literal(mimetype)))

        graph.add(
            (rdflib.URIRef(uri),
             rdflib.URIRef("http://www.w3.org/2000/01/rdf-schema#label"),
             rdflib.URIRef(uri)))

        graph.add(
            (rdflib.URIRef(uri),
             rdflib.URIRef("http://purl.org/dc/terms/created"),
             rdflib.Literal(strftime("%Y-%m-%dT%H:%M:%SZ")))) # FIXME forces zulu time, which may be technically incorrect
        
        rdf = graph.serialize(format="xml") # rdf/xml

        status = self.SPARQLPost(self.files_graph, rdf, "application/rdf+xml")

        logging.debug("Put a webbox:File in the store: "+str(status))

        if status['status'] > 299:
            logging.debug("Put failed: "+str(status))
            return False

        self.add_to_journal(self.files_graph)

        return True


    def _handle_webbox_rdf(self, graph):
        """ Check if this RDF graph contains any webbox trigger RDF, i.e. sioc:to_address, webbox:subscribe, etc and deal with it. """

        handler = WebBoxHandler(graph, self)
        return handler.handle_all()


    def do_PUT(self, rfile, environ, req_path, req_qs):
        """ Handle a PUT. """
        # PUT of RDF is to REPLACE the graph
        content_type = "application/rdf+xml"

        put_uri = self.server_url + req_path
        logging.debug("PUT of uri: %s" % put_uri)

        if environ.has_key("HTTP_TRANSFER_ENCODING") and environ['HTTP_TRANSFER_ENCODING'].lower() == "chunked":
            # part-file is being uploaded, deal with this slightly differently
            size = int(environ['HTTP_X_EXPECTED_ENTITY_LENGTH'])
            content_type = ""
        else:
            size = 0
            if environ.has_key("CONTENT_LENGTH"):
                size = int(environ['CONTENT_LENGTH'])
            if environ.has_key("CONTENT_TYPE"):
                content_type = environ['CONTENT_TYPE']

        # check for a hidden file
        path_parts = req_path.split("/")
        hidden_file = False
        if len(path_parts) > 0 and len( path_parts[ len(path_parts) - 1 ]) > 0 and path_parts[ len(path_parts) - 1 ][0] == ".":
            hidden_file = True

        # if a .rdf is uploaded, set the content-type manually
        if req_path[-4:] == ".rdf":
            content_type = "application/rdf+xml"
        elif req_path[-3:] == ".n3":
            content_type = "text/turtle"
        elif req_path[-3:] == ".nt":
            content_type = "text/plain"

        file_path = self.get_file_path(req_path)
        logging.debug("file path is: %s" % file_path)
        exists = os.path.exists(file_path)

        file = ""
        # parse RDF, but not if it's hidden (hidden is usually a small file from Finder's WebDAV)
        if content_type in self.rdf_formats and not hidden_file:
            # this is an RDF upload

            if size > 0:
                # read into file
                file = rfile.read(size)
                
            # prepare the arguments for local PUTing of this data
            graph = put_uri
            if req_qs.has_key('graph'):
                graph = req_qs['graph'][0]

            # do SPARQL PUT
            logging.debug("WebBox SPARQL PUT to graph (%s)" % (graph) )

            response = self.SPARQLPut(graph, file, content_type)
            if response['status'] > 299:
                return {"data": "", "status": response['status'], "reason": response['reason']}

            self.add_to_journal(graph)

            # replace the file with RDF/XML (the default format for resolving URIs), so convert if we have to
            query = "CONSTRUCT {?s ?p ?o} WHERE { GRAPH <%s> {?s ?p ?o}}" % graph
            result = self.query_store.query(query, {"Accept": "application/rdf+xml"})
            rdf = result['data']

            # write the RDF/XML to the file
            f = open(file_path, "w")
            f.write(rdf)
            f.close()
        else:
            # Handle a static file PUT
            try:
                # check if path exists first
                path = os.path.split(file_path)[0] # folder path without filename
                if not os.path.exists(path):
                    os.makedirs(path)

                f = open(file_path, "w")
                if size > 0:
                    if file == "":
                        # copy straight from rfile
                        shutil.copyfileobj(rfile, f, size)
                    else:
                        # already loaded (by RDF handler, above), write directly
                        f.write(file)
                f.close()

                # the [1:] gets rid of the /webbox/ bit of the path, so FIXME be more intelligent?
                self.add_new_file(os.sep.join(os.path.split(req_path)[1:])) # add metadata to store TODO handle error on return false
            except Exception as e:
                logging.debug(str( "Error writing to file: %s, exception is: %s" % (file_path, str(e)) ) + traceback.format_exc())
                return {"data": "", "status": 500, "reason": "Internal Server Error"}

        # no adding to journal here, because it's a file
        if exists:
            return {"data": "", "status": 204, "reason": "No Content"}
        else:
            return {"data": "", "status": 201, "reason": "Created"}
            

    def SPARQLPut(self, graph, file, content_type):
        """ Handle a SPARQL PUT request. 'graph' is for 4store, 'filename' is for RWW. """

        # send file to query store
        logging.debug("replacing graph %s with rdf" % graph)
        response1 = self.query_store.put_rdf(file, content_type, graph)

        return response1


    def SPARQLPost(self, graph, file, content_type):
        """ Handle a SPARQL POST (append) request. 'graph' is for 4store, 'filename' is for RWW. """

        # send file to query store (4store)
        logging.debug("POST to query store.")
        response1 = self.query_store.post_rdf(file, content_type, graph)

        return response1

    def get_file_path(self, req_path):
        """ Get the file path on disk specified by the request path, or exception if there has been a (security etc.) issue. """

        file_path = os.path.abspath(self.file_dir + os.sep + req_path)

        if not self.check_file_path(file_path):
            raise ResponseOverride(403, "Forbidden")

        return file_path


    def check_file_path(self, path):
        """ Check that a path doesn't contain any references to parent path. """

        # check that the path is within our file directory
        abs_file_path = os.path.abspath(self.file_dir)
        abs_this_path = os.path.abspath(path)

        return abs_this_path.startswith(abs_file_path) and len(abs_this_path) > len(abs_file_path)

