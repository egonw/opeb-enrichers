#!/usr/bin/python

import sys
import json
import time
import re
import configparser

from SPARQLWrapper import SPARQLWrapper, JSON, POSTDIRECTLY
import datetime

from typing import overload, Tuple, List, Dict, Any, Iterator

from .abstract_pub_enricher import AbstractPubEnricher

from .pub_cache import PubDBCache

from . import pub_common

def _extractYear(pubdateStr):
	if pubdateStr[0] == 't':
		pubdate = datetime.datetime.fromtimestamp(float(pubdateStr[1:]), datetime.timezone.utc)
	else:
		pubdate = datetime.datetime.strptime( pubdateStr, "%Y-%m-%dT%H:%M:%SZ" )
	return pubdate.year

class WikidataEnricher(AbstractPubEnricher):
	@overload
	def __init__(self,cache:str=".",prefix:str=None,config:configparser.ConfigParser=None,debug:bool=False):
		...
	
	@overload
	def __init__(self,cache:PubDBCache,prefix:str=None,config:configparser.ConfigParser=None,debug:bool=False):
		...
	
	def __init__(self,cache,prefix:str=None,config:configparser.ConfigParser=None,debug:bool=False):
		#self.debug_cache_dir = os.path.join(cache_dir,'debug')
		#os.makedirs(os.path.abspath(self.debug_cache_dir),exist_ok=True)
		#self._debug_count = 0
		
		super().__init__(cache,prefix,config,debug)
		
		# The section name is the symbolic name given to this class
		section_name = self.Name()
		
		self.wikidata_step_size = self.config.getint(section_name,'wikidata_step_size',fallback=self.step_size)
		
		self.wikidata_query_step_size = self.config.getint(section_name,'wikidata_query_step_size',fallback=self.wikidata_step_size)
	
	# Do not change this constant!!!
	WIKIDATA_SOURCE='wikidata'
	WIKIDATA_SPARQL_ENDPOINT='https://query.wikidata.org/sparql'
	
	@classmethod
	def Name(cls) -> str:
		return cls.WIKIDATA_SOURCE
	
	def populatePubIdsBatch(self,mappings:List[Dict[str,Any]]) -> None:
		sparql = SPARQLWrapper(self.WIKIDATA_SPARQL_ENDPOINT)
		sparql.setRequestMethod(POSTDIRECTLY)
		populateQuery = """
SELECT	?internal_id
	?internal_idLabel
	?pubmed_id
	?doi_id
	?pmc_id
	(GROUP_CONCAT(?author ; SEPARATOR=";") as ?authors)
	?publication_date
	?journal
WHERE {{
	# The query values will go here
	VALUES (?internal_id) {{
		#(<http://www.wikidata.org/entity/Q38485402>)
{0}
	}}
	# Ignoring the class
	?internal_id wdt:P31 ?internal_id_class .
	OPTIONAL {{ ?internal_id wdt:P698 ?pubmed_id. }}
	OPTIONAL {{ ?internal_id wdt:P356 ?doi_id. }}
	OPTIONAL {{ ?internal_id wdt:P932 ?pmc_id. }}
	OPTIONAL {{ ?internal_id wdt:P2093 ?author. }}
	OPTIONAL {{ ?internal_id wdt:P577 ?publication_date. }}
	OPTIONAL {{
		?internal_id wdt:P1433 ?journal_id.
		?journal_id wdt:P1476 ?journal .
	}}
	SERVICE wikibase:label {{
		bd:serviceParam wikibase:language "en".
	}}
}} GROUP BY ?internal_id ?internal_idLabel ?pubmed_id ?doi_id ?pmc_id ?publication_date ?journal
""".format("\n".join(("\t( <"+mapping['id']+"> )"  for mapping in mappings)))
		
		if self._debug:
			print(populateQuery,file=sys.stderr)
			sys.stderr.flush()
		
		sparql.setQuery(populateQuery)
		sparql.setReturnFormat(JSON)
		results = sparql.query().convert()
		
		# Avoiding to hit the server too fast
		time.sleep(self.request_delay)
		
		internal_ids_dict = { mapping['id']: mapping  for mapping in mappings }
		for result in results["results"]["bindings"]:
			_id = result['internal_id']['value']
			mapping = internal_ids_dict.get(_id)
			
			titleV = result.get('internal_idLabel')
			mapping['title'] = titleV['value']  if titleV else None
			
			journalV = result.get('journal')
			mapping['journal'] = journalV['value']  if journalV else None
			
			pubdateV = result.get('publication_date')
			pubyear = _extractYear(pubdateV['value']) if pubdateV else None
			
			mapping['year'] = pubyear
			
			authorsV = result.get('authors')
			mapping['authors'] = authorsV['value'].split(';')  if authorsV and authorsV['value'] != '' else []
			
			pubmed_idV = result.get('pubmed_id')
			# Let's sanitize the id
			mapping['pmid'] = pubmed_idV['value'].strip()  if pubmed_idV else None
			
			doi_idV = result.get('doi_id')
			# Let's sanitize the id
			mapping['doi'] = doi_idV['value'].strip()  if doi_idV else None
			
			pmc_idV = result.get('pmc_id')
			# Let's sanitize the id
			mapping['pmcid'] = pub_common.normalize_pmcid(pmc_idV['value'].strip())  if pmc_idV else None
		

	def queryPubIdsBatch(self,query_ids:List[Dict[str,str]]) -> List[Dict[str,Any]]:
		# Preparing the query ids
		raw_query_pubmed_ids = []
		raw_query_doi_ids = []
		raw_query_pmc_ids = []
		
		for query_id in query_ids:
			pubmed_id = query_id.get('pmid')
			if pubmed_id is not None:
				raw_query_pubmed_ids.append(pubmed_id)
			
			doi_id_norm = query_id.get('doi')
			if doi_id_norm is not None:
				raw_query_doi_ids.append(doi_id_norm)
			
			pmc_id_norm = query_id.get('pmcid')
			if pmc_id_norm is not None:
				pmc_id_wikidata = pub_common.denormalize_pmcid(pmc_id_norm)
				raw_query_pmc_ids.append(pmc_id_wikidata)
		
		# Preparing the query by the different ids
		union_query = []
		if raw_query_pubmed_ids:
			raw_query_pubmed = """
		SELECT DISTINCT ?internal_id
		WHERE {{
			VALUES (?query_pubmed_id) {{
				{0}
			}}
			?internal_id wdt:P698 ?query_pubmed_id.
		}}
""".format("\n".join(map(lambda pubmed_id: '("'+pubmed_id.replace('"','\\"')+'")', raw_query_pubmed_ids)))
			union_query.append(raw_query_pubmed)
		
		if raw_query_doi_ids:
			raw_query_doi = """
		SELECT DISTINCT ?internal_id
		WHERE {{
			VALUES (?query_doi_id) {{
				{0}
			}}
			?internal_id wdt:P356 ?query_doi_id.
		}}
""".format("\n".join(map(lambda pubmed_id: '("'+pubmed_id.replace('"','\\"')+'")', raw_query_doi_ids)))
			union_query.append(raw_query_doi)
		
		if raw_query_pmc_ids:
			raw_query_pmc = """
		SELECT DISTINCT ?internal_id
		WHERE {{
			VALUES (?query_pmc_id) {{
				{0}
			}}
			?internal_id wdt:P932 ?query_pmc_id.
		}}
""".format("\n".join(map(lambda pmc_id: '("'+pmc_id.replace('"','\\"')+'")', raw_query_pmc_ids)))
			union_query.append(raw_query_pmc)
		
		if len(union_query)>0:
			# Now, with the unknown ones, let's ask the server
			sparql = SPARQLWrapper(self.WIKIDATA_SPARQL_ENDPOINT)
			sparql.setRequestMethod(POSTDIRECTLY)
			if len(union_query) == 1:
				# No additional wrap is needed
				queryQuery = union_query[0]
			else:
				# Prepared the union query
				union_q = "\n\t} UNION {\n".join(union_query)
				
				queryQuery = """
SELECT	DISTINCT ?internal_id
WHERE {{
	{{
	{0}
	}}
}}
""".format(union_q)
			if(self._debug):
				print(queryQuery,file=sys.stderr)
				sys.stderr.flush()
			sparql.setQuery(queryQuery)
			sparql.setReturnFormat(JSON)
			results = sparql.query().convert()
			
			# Avoiding to hit the server too fast
			time.sleep(self.request_delay)
			
			mapping_ids = [ result['internal_id']['value']  for result in results["results"]["bindings"] ]
		else:
			mapping_ids = []
		
		# The default return value
		mappings = []
		
		if len(mapping_ids) > 0:
			populateQuery = """
SELECT	?internal_id
	?internal_idLabel
	?pubmed_id
	?doi_id
	?pmc_id
	(GROUP_CONCAT(?author ; SEPARATOR=";") as ?authors)
	?publication_date
	?journal
WHERE {{
	# The query values will go here
	VALUES (?internal_id) {{
		#(<http://www.wikidata.org/entity/Q38485402>)
{0}
	}}
	# Ignoring the class
	?internal_id wdt:P31 ?internal_id_class .
	OPTIONAL {{ ?internal_id wdt:P698 ?pubmed_id. }}
	OPTIONAL {{ ?internal_id wdt:P356 ?doi_id. }}
	OPTIONAL {{ ?internal_id wdt:P932 ?pmc_id. }}
	OPTIONAL {{ ?internal_id wdt:P2093 ?author. }}
	OPTIONAL {{ ?internal_id wdt:P577 ?publication_date. }}
	OPTIONAL {{
		?internal_id wdt:P1433 ?journal_id.
		?journal_id wdt:P1476 ?journal .
	}}
	SERVICE wikibase:label {{
		bd:serviceParam wikibase:language "en".
	}}
}} GROUP BY ?internal_id ?internal_idLabel ?pubmed_id ?doi_id ?pmc_id ?publication_date ?journal
""".format("\n".join(("\t( <"+mapping_id+"> )"  for mapping_id in mapping_ids)))
			
			if(self._debug):
				print(populateQuery,file=sys.stderr)
				sys.stderr.flush()
			sparql.setQuery(populateQuery)
			sparql.setReturnFormat(JSON)
			results = sparql.query().convert()
			
			# Avoiding to hit the server too fast
			time.sleep(self.request_delay)
			
			for result in results["results"]["bindings"]:
				mapping = {
					'id': result['internal_id']['value'],
					'source': self.WIKIDATA_SOURCE
				}
				
				titleV = result.get('internal_idLabel')
				mapping['title'] = titleV['value']  if titleV else None
				
				journalV = result.get('journal')
				mapping['journal'] = journalV['value']  if journalV else None
				
				pubdateV = result.get('publication_date')
				pubyear = _extractYear(pubdateV['value']) if pubdateV else None
				
				mapping['year'] = pubyear
				
				authorsV = result.get('authors')
				mapping['authors'] = authorsV['value'].split(';')  if authorsV and authorsV['value'] != '' else []
				
				pubmed_idV = result.get('pubmed_id')
				# Let's sanitize the id
				mapping['pmid'] = pubmed_idV['value'].strip()  if pubmed_idV else None
				
				doi_idV = result.get('doi_id')
				# Let's sanitize the id
				mapping['doi'] = doi_idV['value'].strip()  if doi_idV else None
				
				pmc_idV = result.get('pmc_id')
				# Let's sanitize the id
				mapping['pmcid'] = pub_common.normalize_pmcid(pmc_idV['value'].strip())  if pmc_idV else None
				
				mappings.append(mapping)
		
		return mappings
	
	def queryCitRefsBatch(self,query_citations_data:Iterator[Dict[str,Any]],minimal:bool=False,mode:int=3) -> Iterator[Dict[str,Any]]:
		"""
		This method always issues the search for both citations
		and references because it is easier (and it is not
		computationally expensive)
		"""
		# First, saving the queries to issue
		results = []
		for query in query_citations_data:
			_id = query['id']
			# Initializing the values
			result = {
				'id': _id,
				'source': query['source']
			}
			for citrefs_key, citrefs_count_key in (pub_common.REFERENCES_KEYS,pub_common.CITATIONS_KEYS):
				result.setdefault(citrefs_key,[])
				result.setdefault(citrefs_count_key,0)
			
			results.append(result)
		
		sparql = SPARQLWrapper(self.WIKIDATA_SPARQL_ENDPOINT)
		sparql.setRequestMethod(POSTDIRECTLY)
		
		def _queryAndProcessCitRefs(theQuery,theStoreKeys,results_hash):
			citrefs_key, citrefs_count_key = theStoreKeys
			
			if(self._debug):
				print(theQuery,file=sys.stderr)
			
			sparql.setQuery(theQuery)
			sparql.setReturnFormat(JSON)
			sparql_results = sparql.query().convert()
			
			# Avoiding to hit the server too fast
			time.sleep(self.request_delay)
			
			for res in sparql_results["results"]["bindings"]:
				internal_id = res['internal_id']['value']
				citref_id = res['_id']['value']
				
				citref_dateV = res.get('_id_date')
				citref_year = _extractYear(citref_dateV['value']) if citref_dateV else None
				
				result = results_hash[internal_id]
				result[citrefs_key].append(
					{
						'id': citref_id,
						'source': self.WIKIDATA_SOURCE,
						'year': citref_year
					}
				)
				result[citrefs_count_key] += 1
		

		# Second, query by batches
		for start in range(0,len(results),self.wikidata_step_size):
			stop = start+self.wikidata_step_size
			
			results_slice = results[start:stop]
			results_slice_hash = { result['id']: result  for result in results_slice }
			
			raw_ids_slice = [ result['id']  for result in results_slice ]
			raw_ids_slice_prepared = "\n".join(("\t( <"+_id+"> )"  for _id in raw_ids_slice))
			
			refsQuery = """
SELECT	?internal_id (?ref_id as ?_id) (?ref_publication_date as ?_id_date)
WHERE {{
	# The query values will go here
	VALUES (?internal_id) {{
		#(<http://www.wikidata.org/entity/Q38485402>)
{0}
	}}
	?internal_id wdt:P2860 ?ref_id.
	OPTIONAL {{ ?ref_id wdt:P577 ?ref_publication_date. }}
}}
""".format(raw_ids_slice_prepared)

			#Enrich with the references
			_queryAndProcessCitRefs(refsQuery, pub_common.REFERENCES_KEYS, results_slice_hash)
			
			citsQuery = """
SELECT	?internal_id (?cit_id as ?_id) (?cit_publication_date as ?_id_date)
WHERE {{
	# The query values will go here
	VALUES (?internal_id) {{
		#(<http://www.wikidata.org/entity/Q38485402>)
{0}
	}}
	# Ignoring the class
	?cit_id wdt:P2860 ?internal_id.
	OPTIONAL {{ ?cit_id wdt:P577 ?cit_publication_date. }}
}}
""".format(raw_ids_slice_prepared)
			
			# And now with the citations
			_queryAndProcessCitRefs(citsQuery, pub_common.CITATIONS_KEYS, results_slice_hash)
			
			# Emitting the already processed results
			for result in results_slice:
				yield result
