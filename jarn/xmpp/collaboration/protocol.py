import logging

from twisted.words.protocols.jabber.xmlstream import IQ
from twisted.words.protocols.jabber.xmlstream import toResponse
from twisted.words.xish.domish import Element
from zope.interface import implements
from wokkel import disco, iwokkel
from wokkel.subprotocols import XMPPHandler

from jarn.xmpp.collaboration.interfaces import IDifferentialSyncronisation
from jarn.xmpp.collaboration.dmp import diff_match_patch

NS_CE = 'http://jarn.com/ns/collaborative-editing'
IQ_GET = '/iq[@type="get"]'
IQ_SET = '/iq[@type="set"]'
CE_PRESENCE = "/presence"
CE_MESSAGE = "/message/x[@xmlns='%s']" % NS_CE

logger = logging.getLogger('jarn.xmpp.collaboration')


class DSCException(Exception):
    pass


class DifferentialSyncronisationClientProtocol(XMPPHandler):
    """
    Client protocol for Collaborative Editing.
    """
    pass


class DifferentialSyncronisationHandler(XMPPHandler):
    """
    Server protocol for Collaborative Editing.
    """

    implements(IDifferentialSyncronisation, iwokkel.IDisco)

    def __init__(self):
        self.node_participants = {}
        self.participant_nodes = {}
        self.participant_focus = {}
        self.shadow_copies = {}
        self.dmp = diff_match_patch()
        super(DifferentialSyncronisationHandler, self).__init__()

    def connectionInitialized(self):
        self.xmlstream.addObserver(IQ_GET + '/shadowcopy[@xmlns="' + NS_CE + '"]',
                                   self._onGetShadowCopyIQ)
        self.xmlstream.addObserver(IQ_SET + '/patch[@xmlns="' + NS_CE + '"]', self._onPatchIQ)
        self.xmlstream.addObserver(CE_PRESENCE, self._onPresence)
        self.xmlstream.addObserver(CE_MESSAGE, self._onMessage)

        logger.info('Collaboration component connected.')


    def _onPresence(self, presence):
        sender = presence['from']
        type = presence.getAttribute('type')
        if type == 'unavailable':
            if sender in self.participant_nodes:
                for node in self.participant_nodes[sender]:
                    self.node_participants[node].remove(sender)
                    self._sendNodeActionToRecipients('user-left', node, sender, self.node_participants[node])
                    self.userLeft(sender, node)
                    if not self.node_participants[node]:
                        del self.node_participants[node]
                        del self.shadow_copies[node]
                del self.participant_nodes[sender]

            if sender in self.participant_focus:
                del self.participant_focus[sender]
            return

        query = presence.query
        node = ''
        if query:
            node = presence.query.getAttribute('node')
        if not node:
            # Ignore, malformed initial presence
            return

        try:
            text = self.getNodeText(sender, node)
        except DSCException:  # Unauthorized access
            return

        if node not in self.shadow_copies:
            self.shadow_copies[node] = text

        if node in self.node_participants:
            self.node_participants[node].add(sender)
        else:
            self.node_participants[node] = set([sender])

        if sender in self.participant_nodes:
            self.participant_nodes[sender].add(node)
        else:
            self.participant_nodes[sender] = set([node])

        # Send user-joined and other participants focus
        self._sendNodeActionToRecipients('user-joined', node, sender, self.node_participants[node] - set([sender]))

        for participant in (self.node_participants[node] - set([sender])):
            if participant in self.participant_focus and self.participant_focus[participant] == node:
                self._sendNodeActionToRecipients('focus', node, participant, [sender])
        self.userJoined(sender, node)

    def _onMessage(self, message):
        sender = message['from']
        x = message.x
        if x is None:
            return
        for elem in x.elements():
            node = elem['node']
            action = elem['action']
            if action == 'focus' and node in self.shadow_copies:
                self.participant_focus[sender] = node
                recipients = [jid for jid in (self.node_participants[node] - set([sender]))]
                self._sendNodeActionToRecipients('focus', node, sender, recipients)
            elif action == 'save' and node in self.shadow_copies:
                self.setNodeText(sender, node, self.shadow_copies[node])

    def _onGetShadowCopyIQ(self, iq):
        node = iq.shadowcopy['node']
        sender = iq['from']
        try:
            if node not in self.node_participants or \
                sender not in self.node_participants[node]:
                raise DSCException("Unauthorized")
            response = toResponse(iq, u'result')
            sc = response.addElement((NS_CE, u'shadowcopy'), content=self.shadow_copies[node])
            sc['node'] = node
        except DSCException, reason:
            response = toResponse(iq, u'error')
            response.addElement((NS_CE, u'error'), content=reason.message)
        finally:
            self.xmlstream.send(response)

    def _onPatchIQ(self, iq):
        node = iq.patch['node']
        sender = iq['from']
        diff = iq.patch.children[0]
        patches = self.dmp.patch_fromText(diff)
        shadow = self.shadow_copies[node]

        (new_text, res) = self.dmp.patch_apply(patches, shadow)
        if False in res:
            response = toResponse(iq, u'error')
            response.addElement((NS_CE, u'error'), content='Error applying patch.')
            self.xmlstream.send(response)
            logger.error('Patch %s could not be applied on node %s' % \
                         (diff, node))
            return

        response = toResponse(iq, u'result')
        response.addElement((NS_CE, u'success',))
        self.xmlstream.send(response)
        self.shadow_copies[node] = new_text
        for receiver in (self.node_participants[node] - set([sender])):
            self._sendPatchIQ(node, sender, receiver, diff )
        logger.info('Patch from %s applied on %s' % (sender, node))

    def _sendPatchIQ(self, node, sender, receiver, patch):
        def success(result, self):
            pass

        def failure(reason, self):
            logger.info("User %s failed on patching node %s" % (sender, node))

        iq = IQ(self.xmlstream, 'set')
        iq['to'] = receiver
        patch = iq.addElement((NS_CE, 'patch'), content=patch)
        patch['node'] = node
        patch['user'] = sender
        d = iq.send()
        d.addCallback(success, self)
        d.addErrback(failure, self)
        return d

    def _sendNodeActionToRecipients(self, action, node, sender, recipients):
        if not recipients:
            return
        message = Element((None, "message", ))
        x = message.addElement((NS_CE, 'x'))
        item = x.addElement('item')
        item['action'] = action
        item['node'] = node
        item['user'] = sender

        for jid in recipients:
            message['to'] = jid
            self.xmlstream.send(message)

    # Disco
    def getDiscoInfo(self, requestor, target, nodeIdentifier=''):
        """
        Get identity and features from this entity, node.

        This handler supports Collaborative Editing, but only without a
        nodeIdentifier specified.
        """
        if not nodeIdentifier:
            return [disco.DiscoFeature(NS_CE)]
        else:
            return []

    def getDiscoItems(self, requestor, target, nodeIdentifier=''):
        """
        Get contained items for this entity, node.

        This handler does not support items.
        """
        return []

    # Implemented by sub-classing.
    def userJoined(self, user, node):
        """
        Called when a user has joined a CE session.

        This method is to meant to be overriden by components.
        """
        pass

    def userLeft(self, user, node):
        """
        Called when a user has left a CE session.

        This method is to meant to be overriden by components.
        """
        pass

    def getNodeText(self, user, node):
        """
        Returns the text of the node before a CE session is started.

        This method is to meant to be overriden by components.
        """
        pass

    def setNodeText(self, user, node, text):
        """
        Saves the text of the node during/after a CE session.

        This method is to meant to be overriden by components.
        """
        pass
