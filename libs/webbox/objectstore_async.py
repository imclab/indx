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

import logging, psycopg2, os, txpostgres
from twisted.internet.defer import Deferred

class ObjectStoreAsync:
    """ Stores objects in a database, handling import, export and versioning.

        Each ObjectStore has single cursor, so create a new ObjectStore object per thread.
    """

    # redundant with webbox_pg2.create_database 
    @staticmethod
    def initialise(db_name, root_user, root_pass, db_user, db_pass):
        """ Create the user, database, tables, view and functions. """

        # try to connect
        try:
            conn = psycopg2.connect(database = db_name,
                                    user = db_user,
                                    password = db_pass)
            conn.close()
            # worked fine, so do not need to reconnect object store
            return
        except Exception as e:
            # failed, make sure user exists:
            root_conn = psycopg2.connect(user = root_user, password = root_pass)
            root_cur = root_conn.cursor()

            root_cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", [db_user])
            role_exists = root_cur.fetchone()

            if role_exists is None:
                # need to create role
                root_cur.execute("CREATE ROLE %s LOGIN ENCRYPTED PASSWORD '%s' NOSUPERUSER INHERIT NOCREATEDB NOCREATEROLE" % (db_user, db_pass))
                root_conn.commit()

            root_cur.close()
            root_conn.close()

        # try to connect again
        try:
            conn = psycopg2.connect(database = db_name,
                                    user = db_user,
                                    password = db_pass)
            conn.close()
            return
        except Exception as e:
            # failed, make sure db exists:
            root_conn = psycopg2.connect(user = root_user, password = root_pass)
            root_conn.set_isolation_level(psycopg2.extensions.ISOLATION_LEVEL_AUTOCOMMIT)
            root_cur = root_conn.cursor()
            root_cur.execute("CREATE DATABASE %s WITH ENCODING='UTF8' OWNER=%s CONNECTION LIMIT=-1" % (db_name, db_user))
            root_conn.commit()
            root_cur.close()
            root_conn.close()
            

            # load in definition from data/objectstore.sql
            fh_objsql = open(os.path.join(os.path.dirname(__file__),"..","..","data","objectstore.sql")) # FIXME put into config
            objsql = fh_objsql.read()
            fh_objsql.close()

            root_conn = psycopg2.connect(database = db_name, user = root_user, password = root_pass) # reconnect to this new db, and without the isolation level set
            root_cur = root_conn.cursor()
            root_cur.execute(objsql)
            root_conn.commit()
            root_cur.close()
            root_conn.close()


    def __init__(self, conn):
        """
            conn is a postgresql psycopg2 database connection
        """
        self.conn = conn
        # TODO FIXME determine if autocommit has to be off for PL/pgsql support
        self.conn.autocommit = True

    def autocommit(self, value):
        """ Set autocommit on/off, for speeding up large INSERTs. """

        if self.conn.autocommit is False and value is True:
            # if we were in a transaction and now are not, then commit first
            self.conn.commit()

        self.conn.autocommit = value

    def rows_to_json(self, rows):
        """ Serialise results from database view as JSON-LD

            rows - The object(s) to serialise.
        """

        obj_out = {}
        for row in rows:
            (graph_uri_sel, graph_version, triple_order, subject, predicate, obj_value, obj_type, obj_lang, obj_datatype) = row

            if "@version" not in obj_out:
                obj_out["@version"] = graph_version

            if subject not in obj_out:
                obj_out[subject] = {}

            if predicate not in obj_out[subject]:
                obj_out[subject][predicate] = []

            if obj_type == "resource":
                obj_key = "@id"
            elif obj_type == "literal":
                obj_key = "@value"
            else:
                raise Exception("Unknown object type from database {0}".format(obj_type)) # TODO create a custom exception to throw

            obj_struct = {}
            obj_struct[obj_key] = obj_value

            if obj_lang is not None:
                obj_struct["@language"] = obj_lang

            if obj_datatype is not None:
                obj_struct["@type"] = obj_datatype

            obj_out[subject][predicate].append(obj_struct)

        return obj_out


    def get_graphs(self):
        """ Get a list of the graph URIs.
        """
        results_d = Deferred()
        logging.debug("Objectstore get_graphs")

        def rows_cb(rows):
            logging.debug("Objectstore get_graphs rows_cb")
            objs_out = []
            for row in rows:
                graph_uri = row[0]
                objs_out.append(graph_uri)
            results_d.callback(objs_out)

        def cursor_cb(cur):
            logging.debug("Objectstore get_graphs cursor_cb")
            d = cur.execute("SELECT DISTINCT graph_uri FROM wb_v_latest_triples")
            d.addCallback(lambda _: cur.fetchall())
            d.addCallback(rows_cb)

        cursor_cb(self.conn.cursor())
        return results_d

    def get_latest_obj(self, graph_uri, object_uri):
        """ Get the latest version of an object in agraph, as expanded JSON-LD notation.
            graph_uri of the named graph
            object_uri of the object
        """

        result_d = Deferred()

        def rows_cb(rows):
            obj_out = self.rows_to_json(rows)
            obj_out["@graph"] = graph_uri
            result_d.callback(obj_out)
        
        def cursor_cb(cur):
            d = cur.execute("SELECT graph_uri, graph_version, triple_order, subject, predicate, obj_value, obj_type, obj_lang, obj_datatype FROM wb_v_latest_triples WHERE graph_uri = %s AND subject = %s", [graph_uri, object_uri]) # order is implicit, defined by the view, so no need to override it here
            d.addCallback(lambda _: cur.fetchall())
            d.addCallback(rows_cb)
 
        cursor_cb(self.conn.cursor())
        return result_d
    

    def get_latest(self, graph_uri):
        """ Get the latest version of a graph, as expanded JSON-LD notation.
            uri of the named graph
        """
        result_d = Deferred()

        def row_cb(rows, version):
            obj_out = self.rows_to_json(rows)
            obj_out["@graph"] = graph_uri
            obj_out["@version"] = version
            result_d.callback(obj_out)

        def rows_cb(rows,cur):
            if rows is None:
                return result_d.callback({"@graph" : graph_uri, "@version": 0 })            
            version = rows[0]
            rowd = cur.execute("SELECT graph_uri, graph_version, triple_order, subject, predicate, obj_value, obj_type, obj_lang, obj_datatype FROM wb_v_latest_triples WHERE graph_uri = %s", [graph_uri]) # order is implicit, defined by the view, so no need to override it here
            rowd.addCallback(lambda _: cur.fetchall())
            rowd.addCallback(lambda rows: row_cb(rows,version))

        def cursor_cb(cur):
            d = cur.execute("SELECT latest_version FROM wb_v_latest_graphvers WHERE graph_uri = %s", [graph_uri])
            d.addCallback(lambda _: cur.fetchone())
            d.addCallback(lambda rows: rows_cb(rows,cur)) 

        cursor_cb(self.conn.cursor())
        return result_d


    def add(self, graph_uri, objs, specified_prev_version):
        """ Add new objects, or new versions of objects, to a graph in the database.

            graph_uri of the named graph,
            objs, json expanded notation of objects in the graph,
            specified_prev_version of the named graph (must match max(version) of the graph, or zero if the object doesn't exist, or the store will return a IncorrectPreviousVersionException

            returns information about the new version
        """

        # TODO FIXME XXX lock the table(s) as appropriate inside a transaction (PL/pgspl?) here
        result_d = Deferred()
        logging.debug("Objectstore add")

        def added_cb(info): # self is the deferred
            new_version, graph_uri = info
            logging.debug("added_cb")
            result_d.callback({"@version": new_version, "@graph": graph_uri})

        def row_cb(row):
            logging.debug("Objectstore add row_cb, row: " + str(row))

            if row is None:
                actual_prev_version = 0
            else:
                actual_prev_version = row[0]

            if actual_prev_version != specified_prev_version:
                ipve = IncorrectPreviousVersionException("Actual previous version is {0}, specified previous version is: {1}".format(actual_prev_version, specified_prev_version))
                ipve.version = actual_prev_version
                raise ipve

            d = self.add_graph_version(graph_uri, objs, actual_prev_version)
            d.addCallback(added_cb)

        def cursor_cb(cur):
            logging.debug("Objectstore add cursor_cb")
            d = cur.execute("SELECT latest_version FROM wb_v_latest_graphvers WHERE graph_uri = %s", [graph_uri])
            d.addCallback(lambda _: cur.fetchone())
            d.addCallback(row_cb)

        cursor_cb(self.conn.cursor())
        return result_d


    def add_graph_version(self, graph_uri, objs, version):
        """ Add new version of a graph.
        """

        result_d = Deferred()
        logging.debug("Objectstore add_graph_version")

        # TODO FIXME XXX lock the table(s) as appropriate inside a transaction (PL/pgspl?) here

        def cursor_cb(cur):
            logging.debug("Objectstore add_graph_version cursor_cb")

            # TODO add this
            id_user = 1

            def row_cb(id_graphver):
                logging.debug("Objectstore add_graph_version row_cb")

                triple_order = 0 # for the whole graph
                queries = []
                for obj in objs:
                    
                    if "@id" in obj:
                        uri = obj["@id"]
                    else:
                        raise Exception("@id required in all objects")

                    for predicate in obj:
                        if predicate[0] == "@":
                            continue # skip over json_ld predicates

                        sub_objs = obj[predicate]
                        for object in sub_objs:
                            if "@value" in object:
                                type = "literal"
                                value = object["@value"]
                            elif "@id" in object:
                                type = "resource"
                                value = object["@id"]

                            language = ''
                            if "@language" in object:
                                language = object["@language"]

                            datatype = ''
                            if "@type" in object:
                                datatype = object["@type"]

                            triple_order += 1
                            queries.append( ("SELECT * FROM wb_add_triple_to_graphvers(%s, %s, %s, %s, %s, %s, %s, %s)", [id_graphver, uri, predicate, value, type, language, datatype, triple_order]) )
        
                def exec_queries(var):
                    logging.debug("Objectstore add_graph_version exec_queries")

                    if len(queries) < 1:
                        result_d.callback((version+1, graph_uri))
                        return
                    
                    (query, params) = queries.pop(0)
                    d = cur.execute(query, params)
                    d.addCallback(exec_queries)

                exec_queries(None)

            d = cur.execute("SELECT * FROM wb_get_graphvers_id(%s, %s, %s)", [version, graph_uri, id_user])
            d.addCallback(lambda _: cur.fetchone())
            d.addCallback(row_cb)

        cursor_cb(self.conn.cursor())
        return result_d




class IncorrectPreviousVersionException(BaseException):
    """ The specified previous version did not match the actual previous version. """
    pass


from rdflib import Graph, URIRef, Literal

class RDFObjectStore:
    """ Uses the query store interface (e.g. as a drop-in replacement for fourstore).
        but asserts/reads data from the objectstore.
    """

    def __init__(self, objectstore):
        self.objectstore = objectstore

        # mime type to rdflib formats
        self.rdf_formats = {
            "application/rdf+xml": "xml",
            "application/n3": "n3",
            "text/turtle": "n3", # no turtle-specific parser in rdflib ATM, using N3 one because N3 is a superset of turtle
            "text/plain": "nt",
            "application/json": "json-ld",
            "text/json": "json-ld",
        }

    def put_rdf(self, rdf, content_type, graph):
        """ Public method to PUT RDF into the store - where PUT replaces a graph. """

        version = 0 # FIXME XXX
        objs = self.rdf_to_objs(rdf, content_type)
        self.objectstore.add(graph, objs, version)

        return {"data": "", "status": 200, "reason": "OK"} 
        


    def rdf_to_objs(self, rdf, content_type):
        """ Convert rdf string of a content_type to an array of objects in JSON-LD expanded format as used in objectstore. """

        rdf_type = self.rdf_formats[content_type]
        rdfgraph = Graph()
        rdfgraph.parse(data=rdf, format=rdf_type) # format = xml, n3 etc

        all_obj = {}
        for (s, p, o) in rdfgraph:
            subject = unicode(s)
            predicate = unicode(p)

            if subject not in all_obj:
                all_obj[subject] = {}
            if predicate not in all_obj[subject]:
                all_obj[subject][predicate] = []

            object_value = unicode(o)
            object = {}

            if type(o) is type(Literal("")):
                typekey = "@value"

                if o.language is not None:
                    object["@language"] = o.language
                if o.datatype is not None:
                    object["@type"] = o.datatype
            else:
                typekey = "@id"
            
            object[typekey] = object_value


            all_obj[subject][predicate].append(object)
       
        objs = []
        for subject in all_obj:
            obj = all_obj[subject]
            obj["@id"] = subject
            objs.append(obj)

        return objs


    def post_rdf(self, rdf, content_type, graph):
        """ Public method to POST RDF into the store - where POST appends to a graph. """

        latest = self.objectstore.get_latest(graph)
        version = latest["@version"] # FIXME ok?

        objs = self.rdf_to_objs(rdf, content_type)

        # include existing objs
        for key in latest:
            if key[0] != "@":
                obj = latest[key]
                obj["@id"] = key
                objs.append(obj)

        self.objectstore.add(graph, objs, version)

        return {"data": "", "status": 200, "reason": "OK"} 

