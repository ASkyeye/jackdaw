
import os
from jackdaw.dbmodel.spnservice import JackDawSPNService
from jackdaw.dbmodel.adacl import JackDawADACL
from jackdaw.dbmodel.adgroup import JackDawADGroup
from jackdaw.dbmodel.adinfo import JackDawADInfo
from jackdaw.dbmodel.aduser import JackDawADUser
from jackdaw.dbmodel.adcomp import JackDawADMachine
from jackdaw.dbmodel.usergroup import JackDawGroupUser
from jackdaw.dbmodel.adinfo import JackDawADInfo
from jackdaw.dbmodel.tokengroup import JackDawTokenGroup
from jackdaw.dbmodel import *

from msldap.core.msldap import *
from msldap.ldap_objects import *

class LDAPEnumerator:
	def __init__(self, db_conn, ldap):
		self.db_con = db_conn
		self.ldap = ldap
		
	def spnservice_enumerator(self):
		ldap_filter = r'(&(sAMAccountType=805306369))'
		attributes = ['sAMAccountName', 'servicePrincipalName']
		
		for entry in self.ldap.pagedsearch(ldap_filter, attributes):
			for spn in entry['attributes']['servicePrincipalName']:			
				port = None
				service, t = spn.rsplit('/',1)
				m = t.find(':')
				if m != -1:
					computername, port = spn.rsplit(':',1)
				else:
					computername = t
					
				s = JackDawSPNService()
				s.computername = computername
				s.service = service
				s.port = port
				yield s
			
	def get_domain_info(self):
		info = self.ldap.get_ad_info()
		return JackDawADInfo.from_dict(info.to_dict())
		
	def get_all_machines(self):
		for machine in self.ldap.get_all_machine_objects():
			yield (machine, JackDawADMachine.from_adcomp(machine))
			
	def get_all_users(self):
		for user in self.ldap.get_all_user_objects():
			#TODO: fix this ugly stuff here...
			if user.sAMAccountName[-1] == "$":
				continue
			yield (user, JackDawADUser.from_aduser(user))

		
	def get_all_groups(self):
		for group in self.ldap.get_all_groups():
			yield JackDawADGroup.from_dict(group.to_dict())

		
	def get_user_effective_memberships(self, user):
		for sid in self.ldap.get_tokengroups(user.dn):
			s = JackDawTokenGroup()
			s.cn = str(user.cn)
			s.dn = str(user.dn)
			
			if isinstance(user, JackDawADUser):
				s.guid = str(user.objectGUID)
				s.sid = str(user.objectSid)
				s.member_sid = sid
				s.is_user = True
			elif isinstance(user, JackDawADMachine):
				s.guid = str(user.objectGUID)
				s.sid = str(user.objectSid)
				s.member_sid = sid
				s.is_machine = True
			elif isinstance(user, JackDawADGroup):
				s.guid = str(user.guid)
				s.sid = str(user.sid)
				s.member_sid = sid
				s.is_group = True
			yield s
			
	def ace_to_dbo(self, sd):
		order_ctr = 0
		for ace in sd.nTSecurityDescriptor.Dacl.aces:
			acl = JackDawADACL()
			acl.ace_order = order_ctr
			order_ctr += 1
			acl.guid = str(sd.objectGUID)
			if sd.objectSid:
				acl.sid = str(sd.objectSid)
			if sd.cn:
				acl.cn = sd.cn
			if sd.distinguishedName:
				acl.dn = str(sd.distinguishedName)
			acl.sd_control = sd.nTSecurityDescriptor.Control
			
			acl.ace_type = ace.Header.AceType.name
			acl.ace_mask = ace.Mask
			t = getattr(ace,'ObjectType', None)
			if t:
				acl.ace_objecttype = str(t)
			
			t = getattr(ace,'InheritedObjectType', None)
			if t:
				acl.ace_inheritedobjecttype = str(t)
			
			acl.ace_sid = str(ace.Sid)
			yield acl
		
	def get_all_acls(self):
		for sd in self.ldap.get_all_objectacl():
			if not sd.nTSecurityDescriptor or not sd.nTSecurityDescriptor.Dacl:
				continue
			
			for acl in self.ace_to_dbo(sd):
				yield acl
				
	def get_acls_for_dn(self, dn):
		for sd in self.ldap.get_objectacl_by_dn(dn):
			if not sd.nTSecurityDescriptor or not sd.nTSecurityDescriptor.Dacl:
				continue
			
			for acl in self.ace_to_dbo(sd):
				yield acl
		
	def get_current_dc_ip(self):
		pass
		#print(os.environ['LOGONSERVER'])
		
	def spn_to_account(self):
		if spn.find('/') != -1:
			return spn.rsplit('/')[1].upper() + '$'
		
	def run(self):
		self.ldap.connect()
		session = get_session(self.db_con)
		info = self.get_domain_info()
		session.add(info)
		session.commit()
		session.refresh(info)
		
		print('Enumerating users...')
		for obj, user in self.get_all_users():
			user.ad_id = info.id
			session.add(user)
			session.commit()
			session.refresh(user)
			
			for membership in self.get_user_effective_memberships(user):
				info.group_lookups.append(membership)
				
			for acl in self.get_acls_for_dn(user.dn):
				acl.ad_id = info.id
				session.add(acl)
			
			for spn in getattr(obj,'allowedtodelegateto',[]):
				con = JackDawUserConstrainedDelegation()
				con.spn = spn
				con.targetaccount = self.spn_to_account(spn)
				user.allowedtodelegateto.append(con)
			
			session.commit()
		
		print('Enumerating machines...')
		for obj, machine in self.get_all_machines():
			machine.ad_id = info.id
			session.add(machine)
			session.commit()
			session.refresh(machine)
			
			for membership in self.get_user_effective_memberships(machine):
				info.group_lookups.append(membership)
			
			for spn in getattr(obj,'allowedtodelegateto',[]):
				con = JackDawMachineConstrainedDelegation()
				con.spn = spn
				con.targetaccount = self.spn_to_account(spn)
				machine.allowedtodelegateto.append(con)
			
			for acl in self.get_acls_for_dn(machine.dn):
				acl.ad_id = info.id
				session.add(acl)
				
			session.commit()
			
		print('Enumerating groups...')
		ctr = 0		
		for group in self.get_all_groups():
			group.ad_id = info.id	
			session.add(group)
			
			for membership in self.get_user_effective_memberships(group):
				info.group_lookups.append(membership)
				
			for acl in self.get_acls_for_dn(group.dn):
				acl.ad_id = info.id
				session.add(acl)
				
			if ctr % 1000 == 0:
				session.commit()
			
		for spnservice in self.spnservice_enumerator():
			spnservice.ad_id = info.id	
			session.add(spnservice)
		session.commit()
		
		"""
		ctr = 0		
		for acl in self.get_all_acls():
			acl.ad_id = info.id
			session.add(acl)
			if ctr % 1000 == 0:
				session.commit()
		"""
		session.commit()
		
		
		
		