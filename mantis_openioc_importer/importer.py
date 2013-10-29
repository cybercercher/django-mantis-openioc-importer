# Copyright (c) Siemens AG, 2013
#
# This file is part of MANTIS.  MANTIS is free software: you can
# redistribute it and/or modify it under the terms of the GNU General Public
# License as published by the Free Software Foundation; either version 2
# of the License, or(at your option) any later version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE.  See the GNU General Public License for more
# details.
#
# You should have received a copy of the GNU General Public License along with
# this program; if not, write to the Free Software Foundation, Inc., 51
# Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.
#


import logging

import re

from django.utils import timezone

from django.utils.dateparse import parse_datetime

from dingos import *

from dingos.core.datastructures import DingoObjDict

from dingos.core.utilities import search_by_re_list, set_dict

from dingos.core.xml_utils import extract_attributes

from mantis_core.import_handling import MantisImporter

from mantis_core.models import FactDataType




from mantis_core.models import Identifier

logger = logging.getLogger(__name__)

class OpenIOC_Import:

    RE_LIST_NS_TYPE_FROM_NS_URL = [re.compile("http://schemas.mandiant.com/(?P<revision>[0-9]+)/(?P<family>[^/]+)")]

    def __init__(self, *args, **kwargs):

        self.toplevel_attrs = {}

        self.namespace_dict = {None:DINGOS_NAMESPACE_URI}

        self.iobject_family_name = 'ioc'
        self.iobject_family_revision_name = ''


        self.create_timestamp = timezone.now()
        self.identifier_ns_uri = DINGOS_DEFAULT_ID_NAMESPACE_URI

    #
    # First of all, we define functions for the hooks provided to us
    # by the DINGO xml-import.
    #



    def id_and_revision_extractor(self,xml_elt):
        """
        Function for generating a unique identifier for extracted embedded content;
        to be used for DINGO's xml-import hook 'embedded_id_gen'.

        In OpenIOC, the identifier is contained in the 'id' attribute of an element;
        the top-level 'ioc' element carries a timestamp in the 'last-modified' attribute.

        """
        result = {'id':None,
                  'timestamp': None}

        attributes = extract_attributes(xml_elt,prefix_key_char='@')

        # Extract identifier:
        if '@id' in attributes:
            result['id']=attributes['@id']

        # Extract time-stamp

        if '@last-modified' in attributes:
            naive = parse_datetime(attributes['@last-modified'])
            # Make sure that information regarding the timezone is
            # included in the time stamp. If it is not, we chose
            # utc as default timezone: if we assume that the same
            # producer of OpenIOC data always uses the same timezone
            # for filling in the 'last-modified' attribute, then
            # this serves the main purpose of time stamps for our
            # means: we can find out the latest revision of a
            # given piece of data.
            if not timezone.is_aware(naive):
                aware = timezone.make_aware(naive,timezone.utc)
            else:
                aware = naive
            result['timestamp']= aware

        return result


    def cybox_embedding_pred(self,parent, child, ns_mapping):
        """
        Predicate for recognizing inlined content in an XML; to
        be used for DINGO's xml-import hook 'embedded_predicate'.

        The generic importer does not recognize anything as embedded.
        """

        # For openIOC, we extract the Indicator-Item elements,
        # since those correspond to observables.

        child_attributes = extract_attributes(child,prefix_key_char='')


        if ('id' in child_attributes and child.name == 'IndicatorItem'):

            # The embedding predicate is supposed to not only return
            # 'True' or 'False', but in case there is an embedding,
            # it should also contain information regarding the type of
            # object that is embedded. This is used, for example, to
            # create the DataType  information for the embedding element
            # (it is a reference to an object of type X).

            # In OpenIOC, The IndicatorItems have the following form::
            #
            #      <IndicatorItem id="b9ef2559-cc59-4463-81d9-52800545e16e" condition="contains">
            #          <Context document="FileItem" search="FileItem/PEInfo/Sections/Section/Name" type="mir"/>
            #          <Content type="string">.stub</Content>
            #      </IndicatorItem>
            #
            # We take the 'document' attribute of the 'Context' element as object type
            # of the embedded object (as we shall see below, upon import, we rewrite
            # the IndicatorItem such that it corresponds to an observable.

            grandchild = child.children
            type_info = None

            while grandchild is not None:
                if grandchild.name == 'Context':
                    context_attributes = extract_attributes(grandchild,prefix_key_char='')
                    if 'document' in context_attributes:
                        type_info = context_attributes['document']
                    break
                grandchild = grandchild.next

            if type_info:
                return type_info
            else:
                return True
        else:
            return False

    def transformer(self,elt_name,contents):
        """
        The OpenIOC indicator contains the actual observable bits of an indicator in the following
        form::

              <IndicatorItem id="b9ef2559-cc59-4463-81d9-52800545e16e" condition="contains">
                   <Context document="FileItem" search="FileItem/PEInfo/Sections/Section/Name" type="mir"/>
                   <Content type="string">.stub</Content>
              </IndicatorItem>

        We would rather have a key-value pairing of the following form (with the 'contains' attribute
        somewhere at the side::

             FileItem/PEInfo/Sections/Section/Name = .stub

        In order to achieve this, we create a DingoObjDict that corresponds to an XML that would
        look like follows::

             <FileItem id="b9ef2559-cc59-4463-81d9-52800545e16e">
              <PEInfo>
               <Sections>
                <Section>
                 <Name condition='contains' type='string'>
                  .stub
                 </Name>
                </Section>
               </Sections>
              </PEInfo>
             </FileItem>

        This is carried out by the transformer function, which is passed to the generic XML importer
        and executed for each element when converting the element into a dictionary structure.
        """

        # If the current element is not an IndicatorItem, we do nothing

        if elt_name != 'IndicatorItem':
            return (elt_name,contents)
        else:
            # We have an indicator item.

            # We initialize the resulting DingoObjDict
            result = DingoObjDict()

            # We initialize the dictionary that will contain the leaf (in the example given
            # above, that would be the dictionary representing the following bit of
            # XML::
            #
            #     <Name condition='contains'>
            #      .stub
            #     </Name>
            #

            leaf = DingoObjDict()

            # We extract the search term and split it into its elements (removing
            # the redundant first element '<something>Item'.)

            (document_type,search_term) = contents['Context']['@search'].split("/",1)
            search_term = search_term.split('/')

            # We extract the data for the leaf dictionary
            search_value = contents['Content']['_value']
            value_type = contents['Content']['@type']
            search_condition = contents['@condition']

            leaf['@value_type'] = value_type
            leaf['@condition'] = search_condition
            leaf['_value'] = search_value


            # We extract the identifier

            item_id = contents['@id']

            result['@id'] = item_id

            # We write the nested dictionary structure:

            set_dict(result,leaf,'set',*search_term)

        return (document_type,result)



    # Next, we define functions for the hooks provided by the
    # 'from_dict' method of DINGO InfoObject Objects.
    #
    # These hook allow us to influence, how information contained
    # in a DingoObjDict is imported into the system.
    #
    # The hooking is carried out by defining a list
    # containing pairs of predicates (i.e., a function returning True or False)
    # and an associated hooking function. For each InfoObject2Fact object
    # (in the first case) resp. each attribute (in the second case),
    # the list is iterated by applying the predicate to input data.
    # If the predicate returns True, then the hooking function is applied
    # and may change the parameters for creation of fact.



    def reference_handler(self,iobject, fact, attr_info, add_fact_kargs):
        """
        Handler for facts that contain a reference to a fact.
        See below in the comment regarding the fact_handler_list
        for a description of the signature of handler functions.

        As shown below in the handler list, this handler is called
        when a attribute with key '@idref' on the fact's node
        is detected -- this attribute signifies that this fact does not contain
        a value but points to another object. Thus, we try to retrieve that
        object from the database. If it exists, fine -- if not, then the
        call to 'create_iobject' returns a PLACEHOLDER object.

        We further create/refer to the fitting fact data type:
        we want the fact data type to express that the fact is
        a reference to an object.
        """

        (namespace_uri,uid) = (self.identifier_ns_uri,attr_info['idref'])


        # We are always able to extract the timestamp from the referencing node, because for OpenIOC,
        # all references are created by DINGO's generic import, and the import writes the timestamp
        # information into the created reference.

        timestamp = attr_info['@timestamp']

        # The following either retrieves an already existing object of given ID and timestamp
        # or creates a placeholder object.

        (target_mantis_obj, existed) = MantisImporter.create_iobject(
            uid=uid,
            identifier_ns_uri=namespace_uri,
            timestamp=timestamp)

        logger.debug("Creation of Placeholder for %s %s returned %s" % (namespace_uri,uid,existed))

        # What remains to be done is to write the reference to the created placeholder object

        add_fact_kargs['value_iobject_id'] = Identifier.objects.get(uid=uid,namespace__uri=namespace_uri)

        # Handlers have to return 'True', otherwise the fact will not be created.

        return True



    def fact_handler_list(self):
        """
        The fact handler list consists of a pairs of predicate and handler function
        If the predicate returns 'True' for a fact to be added to an Information Object,
        the handler function is executed and may modify the parameters that will be passed
        to the function creating the fact.

        The signature of a predicate is as follows:

        - Inputs:
          - fact dictionary of the following form::

               { 'node_id': 'N001:L000:N000:A000',
                 'term': 'Hashes/Hash/Simple_Hash_Value',
                 'attribute': 'condition' / False,
                 'value': u'Equals'
               }
          - attr_info:
            A dictionary with mapping of XML attributes concerning the node in question
            (note that the keys do *not* have a leading '@' unless it is an internally
            generated attribute by Dingo.

        - Output: Based on these inputs, the predicate must return True or False. If True
          is returned, the associated handler function is run.

        The signature of a handler function is as follows:

        - Inputs:

          - info_object: the information object to which the fact is to be added
          - fact: the fact dictionary of the following form::
               { 'node_id': 'N001:L000:N000:A000',
                 'term': 'Hashes/Hash/Simple_Hash_Value',
                 'attribute': 'condition' / False,
                 'value': u'Equals'
               }
          - attr_info:
            A dictionary with mapping of XML attributes concerning the node in question
            (note that the keys do *not* have a leading '@' unless it is an internally
            generated attribute by Dingo.

          - add_fact_kargs:
            The arguments with which the fact will be generated after all handler functions
            have been called. The dictionary contains the following keys::

                'fact_dt_kind' : <FactDataType.NO_VOCAB/VOCAB_SINGLE/...>
                'fact_dt_namespace_name': <human-readable shortname for namespace uri>
                'fact_dt_namespace_uri': <namespace uri for datataype namespace>
                'fact_term_name' : <Fact Term such as 'Header/Subject/Address'>
                'fact_term_attribute': <Attribute key such as 'category' for fact terms describing an attribute>
                'values' : <list of FactValue objects that are the values of the fact to be generated>
                'node_id_name' : <node identifier such as 'N000:N000:A000'

        - Outputs:

          The handler function outputs either True or False: If False is returned,
          then the fact will *not* be generated. Please be aware that if you use this option,
          then there will be 'missing' numbers in the sequence of node ids.
          Thus, if you want to suppress the creation of facts for attributes,
          rather use the hooking function 'attr_ignore_predicate'

          As side effect, the function can make changes to the dictionary passed in parameter
          'add_fact_kargs' and thus change the fact that will be created.

        """

        return [(lambda fact,  attr_info: "idref" in attr_info.keys(),
                 self.reference_handler)]

    def attr_ignore_predicate(self,fact_dict):
        """
        The attr_ignore predicate is called for each fact that would be generated
        for an XML attribute. It takes a fact dictionary of the following form
        as input::
               { 'node_id': 'N001:L000:N000:A000',
                 'term': 'Hashes/Hash/Simple_Hash_Value',
                 'attribute': 'condition',
                 'value': u'Equals'
               }

        If the predicate returns 'False, the fact is *not* created. Note that, nevertheless,
        during import, the information about this attribute is available to
        the attributed fact as part of the 'attr_dict' that is generated for the creation
        of each fact and passed to the handler functions called for the fact.

        """

        if '@' in fact_dict['attribute']:
            # We remove all attributes added by Dingo during import
            return True
        if fact_dict['attribute'] in ['idref','id','value_type']:
            # The attributes idref, id and value_type we have already used during import;
            # there is no need to keep those around.
            return True
        return False

    def datatype_extractor(self,iobject, fact, attr_info, namespace_mapping, add_fact_kargs):
        """

        The datatype extractor is called for each fact with the aim of determining the fact's datatype.
        The extractor function has the following signature:

        - Inputs:
          - info_object: the information object to which the fact is to be added
          - fact: the fact dictionary of the following form::
               { 'node_id': 'N001:L000:N000:A000',
                 'term': 'Hashes/Hash/Simple_Hash_Value',
                 'attribute': 'condition' / False,
                 'value': u'Equals'
               }
          - attr_info:
            A dictionary with mapping of XML attributes concerning the node in question
            (note that the keys do *not* have a leading '@' unless it is an internally
            generated attribute by Dingo.
          - namespace_mapping:
            A dictionary containing the namespace mapping extracted from the imported XML file.
          - add_fact_kargs:
            The arguments with which the fact will be generated after all handler functions
            have been called. The dictionary contains the following keys::

                'fact_dt_kind' : <FactDataType.NO_VOCAB/VOCAB_SINGLE/...>
                'fact_dt_namespace_name': <human-readable shortname for namespace uri>
                'fact_dt_namespace_uri': <namespace uri for datataype namespace>
                'fact_term_name' : <Fact Term such as 'Header/Subject/Address'>
                'fact_term_attribute': <Attribute key such as 'category' for fact terms describing an attribute>
                'values' : <list of FactValue objects that are the values of the fact to be generated>
                'node_id_name' : <node identifier such as 'N000:N000:A000'

        Just as the fact handler functions, the datatype extractor can change the add_fact_kargs dictionary
        and thus change the way in which the fact is created -- usually, this ability is used to change
        the following items in the dictionary:

        - fact_dt_name
        - fact_dt_namespace_uri
        - fact_dt_namespace_name (optional -- the defining part is the uri)
        - fact_dt_kind

        The extractor returns "True" if datatype info was found; otherwise, False is returned
        """

        # for OpenIOC import, we extract data type information in two cases:
        # - in case of a reference to an embedded object, we make the fact data type
        #   a reference type
        # - in case of a 'leaf' element containing the value of an indicator, we use the
        #   'value_type' attribute provided by OpenIOC to extract the data type of that
        #   value.


        if "idref" in attr_info:
            # We are dealing with a reference.
            # Above, in the embedded_predicate, we have extracted type information about
            # the embedded object. The Dingo importer has added an internal attribute
            # with this information to the created reference, which we now extract.

            embedded_type_info = attr_info.get('@embedded_type_info',None)
            if embedded_type_info:
                add_fact_kargs['fact_dt_name'] = embedded_type_info
                add_fact_kargs['fact_dt_namespace_uri'] = namespace_mapping[None]
                add_fact_kargs['fact_dt_kind'] = FactDataType.REFERENCE

            return True

        elif "value_type" in attr_info:
            # If a value_type attribute is given, we extract the data type from this value.
            add_fact_kargs['fact_dt_name'] = attr_info["value_type"]
            add_fact_kargs['fact_dt_namespace_uri'] = namespace_mapping[None]
            return True
        return False


    def xml_import(self,
                   filepath=None,
                   xml_content=None,
                   markings=None,
                   identifier_ns_uri=None,
                   **kwargs):
        """
        Import an OpenIOC indicator xml (root element 'ioc') from file <filepath>.
        You can provide:

        - a list of markings with which all generated Information Objects
           will be associated (e.g., in order to provide provenance function)

        - The uri of a namespace of the identifiers for the generated information objects.
          This namespace identifiers the 'owner' of the object. For example, if importing
          IOCs published by Mandiant (e.g., as part of the APT1 report), chose an namespace
          such  as 'mandiant.com' or similar (and be consistent about it, when importing
          other stuff published by Mandiant).

        The kwargs are not read -- they are present to allow the use of the
        DingoImportCommand class for easy definition of commandline import commands
        (the class passes all command line arguments to the xml_import function, so
        without the **kwargs parameter, an error would occur.
        """

        # Clear state in case xml_import is used several times

        self.__init__()

        # Initialize  default arguments

        # '[]' would be mutable, so we initialize here
        if not markings:
            markings = []

        # Initalizing here allows us to also get the default namespace when
        # explicitly passing 'None' as parameter.

        if identifier_ns_uri:
            self.identifier_ns_uri = identifier_ns_uri


        # Use the generic XML import customized for  OpenIOC import
        # to turn XML into DingoObjDicts

        import_result =  MantisImporter.xml_import(xml_fname=filepath,
                                                   xml_content=xml_content,
                                                   ns_mapping=self.namespace_dict,
                                                   embedded_predicate=self.cybox_embedding_pred,
                                                   id_and_revision_extractor=self.id_and_revision_extractor,
                                                   transformer=self.transformer,
                                                   keep_attrs_in_created_reference=False,
                                                  )



        id_and_rev_info = import_result['id_and_rev_info']
        elt_name = import_result['elt_name']
        elt_dict = import_result['dict_repr']

        embedded_objects = import_result['embedded_objects']

        default_ns = self.namespace_dict.get(elt_dict.get('@@ns',None),'http://schemas.mandiant.com/unknown/ioc')

        # Export family information.
        family_info_dict = search_by_re_list(self.RE_LIST_NS_TYPE_FROM_NS_URL,default_ns)
        if family_info_dict:
            self.iobject_family_name=family_info_dict['family']
            self.iobject_family_revision_name=family_info_dict['revision']


        # Initialize stack with import_results.

        # First, the result from the top-level import
        pending_stack = [(id_and_rev_info, elt_name,elt_dict)]

        # Then the embedded objects
        for embedded_object in  embedded_objects:
            id_and_rev_info = embedded_object['id_and_rev_info']
            elt_name = embedded_object['elt_name']
            elt_dict = embedded_object['dict_repr']
            pending_stack.append((id_and_rev_info,elt_name,elt_dict))

        if id_and_rev_info['timestamp']:
            ts = id_and_rev_info['timestamp']
        else:
            ts = self.create_timestamp

        for (id_and_rev_info, elt_name, elt_dict) in pending_stack:
            # call the importer that turns DingoObjDicts into Information Objects in the database
            iobject_type_name = elt_name
            iobject_type_namespace_uri = self.namespace_dict.get(elt_dict.get('@@ns',None),DINGOS_GENERIC_FAMILY_NAME)

            MantisImporter.create_iobject(iobject_family_name = self.iobject_family_name,
                                          iobject_family_revision_name= self.iobject_family_revision_name,
                                          iobject_type_name=iobject_type_name,
                                          iobject_type_namespace_uri=iobject_type_namespace_uri,
                                          iobject_type_revision_name= '',
                                          iobject_data=elt_dict,
                                          uid=id_and_rev_info['id'],
                                          identifier_ns_uri= identifier_ns_uri,
                                          timestamp = ts,
                                          create_timestamp = self.create_timestamp,
                                          markings=markings,
                                          config_hooks = {'special_ft_handler' : self.fact_handler_list(),
                                                         'datatype_extractor' : self.datatype_extractor,
                                                         'attr_ignore_predicate' : self.attr_ignore_predicate},
                                          namespace_dict=self.namespace_dict,
                                          )








