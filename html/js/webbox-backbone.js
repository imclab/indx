/*global $,_,document,window,console,escape,Backbone,exports */
/*jslint vars:true, todo:true */
/*

  WebBox.js is the JS Client SDK for WebBox WebBox
  which builds upon Backbone's Model architecture.

  CURRENT TODOs:
  	- update only supports updating the entire box
    - Box.fetch() retrieves _entire box contents_
	  ... which is a really bad idea.

  @prerequisites:
	jquery 1.8.0 or higher
	backbone.js 0.9.2 or higher
	underscore.js 1.4.2 or higher

  This file is part of WebBox.
  Copyright 2012 Max Van Kleek, Daniel Alexander Smith
  Copyright 2012 University of Southampton

  WebBox is free software: you can redistribute it and/or modify
  it under the terms of the GNU General Public License as published by
  the Free Software Foundation, either version 3 of the License, or
  (at your option) any later version.

  WebBox is distributed in the hope that it will be useful,
  but WITHOUT ANY WARRANTY; without even the implied warranty of
  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
  GNU General Public License for more details.

  You should have received a copy of the GNU General Public License
  along with WebBox.  If not, see <http://www.gnu.org/licenses/>.
*/

(function(){
	// intentional fall-through to window if running in a browser
	"use strict";
	var root = this, WebBox;
	var NAMEPREFIX = true;
	// The top-level namespace
	if (typeof exports !== 'undefined'){ WebBox = exports.WebBox = {};	}
	else { WebBox = root.WebBox = {}; }

	 // utilities -----------------> should move out to utils
	var u; // to be filled in by dependency

	// set up our parameters for webbox -
	// default is that we're loading from an _app_ hosted within
	// webbox. 
	var DEFAULT_HOST = document.location.host;
	
	var serialize_obj = function(obj) {
		var uri = obj.id;
		var out_obj = {};
		$.each(obj.attributes, function(pred, vals){
			if (pred[0] === "_" || pred[0] === "@"){
				// don't expand @id etc.
				return;
			}
			var obj_vals = [];
			if (!(vals instanceof Array)){
				vals = [vals];
			}
			$.each(vals, function(){
				var val = this;
				if (val instanceof WebBox.Obj) {
					obj_vals.push({"@id": val.id });
				} else if (typeof val === "object" && (val["@value"] || val["@id"])) {
					// not a WebBox.Obj, but a plan JS Obj
					obj_vals.push(val); // fully expanded string, e.g. {"@value": "foo", "@language": "en" ... }
				} else if (typeof val === "string" || val instanceof String ){
					obj_vals.push({"@value": val});
				} else if (_.isDate(val)) {
					obj_vals.push({"@value": val.toISOString(), "@type":"http://www.w3.org/2001/XMLSchema#dateTime"});
				} else if (_.isNumber(val) && u.isInteger(val)) {
					obj_vals.push({"@value": val.toString(), "@type":"http://www.w3.org/2001/XMLSchema#integer"});
				} else if (_.isNumber(val)) {
					obj_vals.push({"@value": val.toString(), "@type":"http://www.w3.org/2001/XMLSchema#float"});
				} else if (_.isBoolean(val)) {
					obj_vals.push({"@value": val.toString(), "@type":"http://www.w3.org/2001/XMLSchema#boolean"});
				} else {
					u.warn("Could not determine type of val ", val);
					obj_vals.push({"@value": val.toString()});
				}
			});
			out_obj[pred] = obj_vals;
		});
		out_obj['@id'] = uri;
		return out_obj;
	};

	var literal_deserializers = {
		'': function(o) { return o['@value']; },
		"http://www.w3.org/2001/XMLSchema#integer": function(o) { return parseInt(o['@value'], 10); },
		"http://www.w3.org/2001/XMLSchema#float": function(o) { return parseFloat(o['@value'], 10); },
		"http://www.w3.org/2001/XMLSchema#double": function(o) { return parseFloat(o['@value'], 10); },
		"http://www.w3.org/2001/XMLSchema#boolean": function(o) { return o['@value'].toLowerCase() === 'true'; },
		"http://www.w3.org/2001/XMLSchema#dateTime": function(o) { return new Date(Date.parse(o['@value'])); }
	};
	var deserialize_literal = function(obj) {
		return obj['@value'] ? literal_deserializers[ obj['@type'] || '' ](obj) : obj;
	};	
	

	// MAP OF THIS MODUULE :::::::::::::: -----
	// 
	// An Obj is a single instance, thing in WebBox.
	// 
	// A Box is a model that has an attribute called 'Objs'.
	// ...  which is a Backbone.Collection of Graph objects.
	// 
	// A _Store_ represents a single WebBox server, which has an
	//	 attribute called 'boxes' - 
	// ... which is a collection of Box objects

	var Obj = WebBox.Obj = Backbone.Model.extend({
		idAttribute: "@id", // the URI attribute is '@id' in JSON-LD
		initialize:function(attrs, options) {
			this.box = options.box;
		},
		get_id:function() { return this.id;	},			
		_value_to_array:function(k,v) {
			if (k === '@id') { return v; }
			if (!_(v).isUndefined() && !_(v).isArray()) {
				return [v];
			}
			return v;
		},		
		_all_values_to_arrays:function(o) {
			if (!_(o).isObject()) {	console.error(' not an object', o); return o; }
			var this_ = this;
			// ?!?! this isn't doing anything (!!)
			return u.dict(_(o).map(function(v,k) {
						       var val = this_._value_to_array(k,v);
						       if (u.defined(val)) { return [k,val]; }
						       return undefined;
					       }).filter(u.defined));
		},
		set:function(k,v,options) {
			// set is tricky because it can be called like
			// set('foo',123) or set({foo:123})
			if (typeof k === 'string') { v = this._value_to_array(k,v);	}
			else {	k = this._all_values_to_arrays(k);	}
			return Backbone.Model.prototype.set.apply(this,[k,v,options]);
		},
		_delete:function() {
			return this.box._delete_models([this]);
		},
		_fetch:function() {
			var this_ = this, d = u.deferred(), box = this.box.get_id();
			this.box._ajax('GET', box, {'id':this.id}).then(function(response) {
				u.log('query response ::: ', response);
				var version = null;
				var objdata = response.data;
				if (this_.id !== this_.get_id()) { return this_.set('@id', this_.get_id()); }
				$.each(objdata, function(uri, obj){
					// top level keys - corresponding to box level properties
					// set the box id cos 
					if (uri === "@version") { version = obj; return; }
					if (uri[0] === "@") { return; } // ignore "@id" etc
					// not one of those, so must be a
					// < uri > : { prop1 .. prop2 ... }
					$.each(obj, function(key, vals){
						if (key.indexOf('@') === 0) { return; } // ignore "@id" etc
						var obj_vals = vals.map(function(val) {
							// it's an object, so return that
							if (val.hasOwnProperty("@id")) { return this_.get_or_create_obj(val["@id"]); }
							// it's a non-object
							if (val.hasOwnProperty("@value")) {	return deserialize_literal(val); }
							// don't know what it is!
							u.assert(false, "cannot unpack value ", val);
						});
						// only update keys that have changed
						var prev_vals = this_.get(key);
						if ( prev_vals === undefined ||
							 obj_vals.length !== prev_vals.length ||
							  _(obj_vals).difference(prev_vals).length > 0 ||
							  _(prev_vals).difference(obj_vals).length > 0) {
							this_.set(key,obj_vals);
						}
					});
					// we need to ensure our box is up to date otherwise version skew!
					this_.box._update_version_to(version)
						.then(function() { d.resolve(this_); })
						.fail(function(err) { d.reject(err); });
				});

			});
			return d.promise();
		},
		sync: function(method, model, options){
			switch(method){
			case "create": return u.assert(false, "create is never used for Objs"); 
			case "read"  : return model._fetch(); 
			case "update": return model.box._update(model.id);
			case "delete": return model._delete(); 
			}
		}		
	});


	// Box =================================================
	// WebBox.GraphCollection is the list of WebBox.Objs in a WebBox.Graph
	var ObjCollection = Backbone.Collection.extend({ model: Obj });

	// new client: fetch is always lazy, only gets ids, and
	// lazily get objects as you go
	var Box = WebBox.Box = Backbone.Model.extend({
		idAttribute:"@id",
		initialize:function(attributes, options) {
			u.assert(options.store, "no store provided");
			this.store = options.store;
			this.set({objcache: new ObjCollection(), objlist: []});
		},
		get_cache_size:function(i) { return this._objcache().length; },
		_objcache:function() { return this.attributes.objcache; },
		_objlist:function() { return this.attributes.objlist; },
		_set_objlist:function(ol) { return this.set({objlist:ol}); },
		_set_token:function(token) { this.set("token", token);	},
		_set_version:function(v) { this.set("version", v);	},
		_get_version:function(v) { return this.get("version"); },		
		get_token:function() {
			var this_ = this, d = u.deferred();
			this._ajax('POST', 'auth/get_token', { app: this.store.get('app') })
				.then(function(data) {
					this_._set_token( data.token );
					d.resolve(this_);
				}).fail(function(err) { u.error(' error fetching ', this_.id, err); d.reject(err); });
			return d.promise();			
		},
		get_id:function() { return this.id || this.cid;	},
		_ajax:function(method, path, data) {
			data = _(_(data||{}).clone()).extend({box: this.id || this.cid, token:this.get('token')});
			return this.store._ajax(method, path, data);
		},
		query: function(q){
			// @TODO ::::::::::::::::::::::::::
			var d = u.deferred();
			this._ajax(this, "/query", "GET", {"q": JSON.stringify(q)})
				.then(function(data){
					console.debug("query results:",data);
				}).fail(function(data) {
					console.debug("fail query");
				});
			return d.promise();
		},
		_update_version_to:function(version) {
			var d = u.deferred(), box = this.get_id(), this_ = this, cur_version = this._get_version();
			if (version !== undefined && cur_version === version) {
				// if we're already at current version, we have no work to do
				d.resolve();
			} else {
				// otherwise we launch a diff
				console.log('cur version ', cur_version, box);
				this._ajax("GET", [box,'diff'].join('/'), {from_version:cur_version,return_objs:false}).then(
					function(response) {
						// update version
						var latest_version = response['@latest_version'],
							added_ids  = response['@added_ids'],
							changed_ids = response['@changed_ids'],
						    deleted_ids = response['@deleted_ids'],
							all_ids = response['@all_ids'];						
						
						u.assert(latest_version !== undefined, 'latest version not provided');
						u.assert(added_ids !== undefined, 'added_ids not provided');
						u.assert(changed_ids !== undefined, 'changed_ids not provided');
						u.assert(deleted_ids !== undefined, 'deleted _ids not provided');
						
						this_._set_version(latest_version);
						this_._update_object_list(all_ids);						
						
						var cached_changed =
							this_._objcache().filter(function(m) { return changed_ids.indexOf(m.id) >= 0; });
						u.when(cached_changed.map(function(m) { return m.fetch(); }))
							.then(d.resolve).fail(d.reject);						
					});
			}
			return d.promise();			
		},
		_create_model_for_id: function(obj_id){
			var model = new Obj({"@id":obj_id}, {box:this});
			this._objcache().add(model);
			return model;
		},		
		get_obj:function(objid) {
			// get_obj always returns a promise
			var d = u.deferred(), hasmodel = this._objcache().get(objid), this_ = this;
			if (hasmodel !== undefined) {
				d.resolve(hasmodel);
			} else {
				var model = this_._create_model_for_id(objid);
				// if the serve knows about it, then we fetch its definition
				if (this._objlist().indexOf(objid) >= 0) {
					model.fetch().then(d.resolve).fail(d.reject);
				} else {
					// otherwise it must be new!
					model.is_new = true;
					d.resolve(model);
				}
			}
			return d.promise();
		},
		// ----------------------------------------------------
		_update_object_list:function(updated_obj_ids) {
			var current = updated_obj_ids, olds = this.get('objlist').slice(), this_ = this;
			// diff em
			var news = _(current).difference(olds), died = _(olds).difference(current);
			this.set({objlist:current});
			news.map(function(aid) { this_.trigger('obj-add', aid); });
			news.map(function(rid) {
				this_.trigger('obj-remove', rid);
				this_._objcache().remove(rid);
			});
		},		
		_fetch:function() {
			// new client :: this now _only_ fetches object ids
			var box = this.get_id(), d = u.deferred(), this_ = this;
			// return a list of models (each of type WebBox.Object) to populate a GraphCollection
			this._ajax("GET", [box,'get_object_ids'].join('/')).then(
				function(response){
					u.assert(response['@version'] !== undefined, 'no version provided');
					console.log(' BOX FETCH VERSION ', response['@version']);
					this_._set_version(response['@version']);
					this_._update_object_list(response.ids);
					d.resolve(this_);
				}).fail(function(err) { d.reject(err, this_);});
			return d.promise();
		},
		// -----------------------------------------------
		_check_token_and_fetch : function() {
			var this_ = this;
			if (this.get('token') === undefined) {
				var d = u.deferred();
				this.get_token()
					.then(function() { this_._fetch().then(d.resolve).fail(d.reject);	})
					.fail(function(err) { d.reject(err); u.error("FAIL "); });
				return d.promise();
			}
			return this_._fetch();			
		},
		_update:function(ids) {
			ids = ids !== undefined ? (_.isArray(ids) ? ids.slice() : [ids]) : undefined;			
			var d = u.deferred(), version = this.get('version') || 0, this_ = this,
				objs = this._objcache().filter(function(x) {
					return ids === undefined || ids.indexOf(x.id) >= 0;
				}).map(function(obj){ return serialize_obj(obj); });			
			this._ajax("PUT",  this.id + "/update", { version: escape(version), data : JSON.stringify(objs)  })
				.then(function(response) {
					this_.set('version', response.data["@version"]);
					d.resolve(this_);
				}).fail(function(err) {
					// catch obsolete error	- then automatically refetch?
					d.reject(err);
				});
			return d.promise();
		},
		_create_box:function() {
			var d = u.deferred();
			var this_ = this;
			this.store._ajax('POST', 'admin/create_box', { name: this.get_id() } )
				.then(function() {
					this_.fetch().then(function() { d.resolve(); }).fail(function(err) { d.reject(err); });
				}).fail(function(err) { d.reject(err); });
			return d.promise();
		},
		_delete_models:function(models) {
			var version = this.get('version') || 0; 
			var m_ids = models.map(function(x) { return x.id; });
			return this._ajax('DELETE', this.id+'/', { version:version, data: JSON.stringify(m_ids) });
		},
		sync: function(method, model, options){
			console.log('method ', method, model.id);
			switch(method)
			{
			case "create": return model._create_box();
			case "read": return model._check_token_and_fetch(); 
			case "update": return model._update(); 
			case "delete": return u.warn('box.delete() : not implemented yet');
			}
		},
		toString: function() { return 'box:' + this.get_id(); }		
	});
	
	var BoxCollection = Backbone.Collection.extend({ model: Box });
	
	var Store = WebBox.Store = Backbone.Model.extend({
		defaults: {
			server_url: "http://"+DEFAULT_HOST,
			app:"--default-app-id--",
			toolbar:true
		},
		ajax_defaults : {
			jsonp: false, contentType: "application/json",
			xhrFields: { withCredentials: true }
		},
		initialize: function(attributes, options){
			console.log(" server ", this.get('server_url'));
			this.set({boxes : new BoxCollection([], {store: this})});
			// load and launch the toolbar
			if (this.get('toolbar')) { this._load_toolbar(); }
		},
		is_same_domain:function() {
			return this.get('server_url').indexOf(document.location.host) >= 0 &&
				(document.location.port === (this.get('server_port') || ''));
		},
		_load_toolbar:function() {
			var el = $('<div></div>').addClass('unloaded_toolbar').appendTo('body');
			this.toolbar = new WebBox.Toolbar({el: el, store: this});
			this.toolbar.render();
		},
		boxes:function() {	return this.attributes.boxes;	},
		get_box: function(boxid) {	return this.boxes().get(boxid);	},
		get_or_create_box:function(boxid) { return this.boxes().get(boxid) || this._create(boxid);	},
		checkLogin:function() { return this._ajax('GET', 'auth/whoami'); },
		getInfo:function() { return this._ajax('GET', 'admin/info'); },
		login : function(username,password) {
			var d = u.deferred();
			var this_ = this;
			this._ajax('POST', 'auth/login', { username: username, password: password })
				.then(function(l) { this_.trigger('login', username); d.resolve(l); })
				.fail(function(l) { d.reject(l); });			
			return d.promise();
		},
		logout : function() {
			var d = u.deferred();
			var this_ = this;
			this._ajax('POST', 'auth/logout')
				.then(function(l) { this_.trigger('logout'); d.resolve(l); })
				.fail(function(l) { d.reject(l); });
			return d.promise();			
		},
		_create:function(boxid) {
			var b = new Box({}, {store: this});
			b.cid = boxid;
			this.boxes().add(b);
			return b;
		},
		_fetch:function() {
			// fetches list of boxes
			var this_ = this, d = u.deferred();			
			this._ajax('GET','admin/list_boxes')
				.success(function(data) {
					var boxes =	data.list.map(function(boxid) { return this_.get_or_create_box(boxid); });
					this_.boxes().reset(boxes);
					d.resolve(boxes);
				})
				.error(function(e) { d.reject(e); });
			return d.promise();
		},
		_ajax:function(method, path, data) {
			var url = [this.get('server_url'), path].join('/');
			u.log(' store ajax ', method, url);
			var default_data = { app: this.get('app') };
			var options = _(_(this.ajax_defaults).clone()).extend(
				{ url: url, method : method, crossDomain: !this.is_same_domain(), data: _(default_data).extend(data) }
			);
			return $.ajax( options ); // returns a deferred		
		},		
		sync: function(method, model, options){
			switch(method){
			case "create": return u.error('store.create() : cannot create a store'); // TODO
			case "read"  : return model._fetch(); 
			case "update": return u.error('store.update() : cannot update a store'); // TODO
			case "delete": return u.error('store.delete() : cannot delete a store'); // tODO
			}
		}
	});

	var dependencies = [
		'/js/vendor/bootstrap.min.js',
		'/js/vendor/lesscss.min.js',
		'/js/utils.js',
		'/components/toolbar/toolbar.js'
	];
	
	WebBox.load = function(base_url) {
		if (base_url === undefined) {
			base_url = ['http:/', document.location.host].join('/');
		}
		console.log('loading from base url ', base_url);		
		var _load_d = new $.Deferred(); 
		var ds = dependencies.map(function(s) {
			console.log('getting script ', base_url + s);
			return $.getScript(base_url + s);
		});
		$.when.apply($,ds).then(function() {
			u = WebBox.utils;
			console.log('LOADING TOOLBAR');
			WebBox.Toolbar.load_templates(base_url).then(function() {
				_load_d.resolve(root);
			}).fail(function(err) { console.error(err); _load_d.reject(root);   });
		}).fail(function(err) { console.error(err); _load_d.reject(root); });
		return _load_d.promise();
	};
}).call(this);


/**
   moved from box >> 
   ajax : function( path, type, data ) {
   var url = this.store.options.server_url + this.id + path;
   var this_ = this;
   var options = {
   type: type,
   url : url,
   crossDomain: true,
   jsonp: false,
   contentType: "application/json",
   dataType: "json",
   data: _({ token:this_.get('token') }).extend(data),
   xhrFields: { withCredentials: true }
   };
   return $.ajax( options ); // returns a deferred		
   },		
*/
