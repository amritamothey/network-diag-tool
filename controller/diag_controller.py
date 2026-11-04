import json, time
import networkx as nx
from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, set_ev_cls
from ryu.ofproto import ofproto_v1_3
from ryu.lib import hub
from ryu.lib.packet import packet, ethernet, arp, ipv4, ether_types
from ryu.topology import event
from ryu.topology.api import get_switch, get_link
from ryu.app.wsgi import ControllerBase, WSGIApplication, route, Response

CAPACITY_MBPS = 10
CONGESTION_UTIL = 0.8
LOSS_THRESHOLD_PCT = 20
RTT_THRESHOLD_MS = 120
DEGRADE_HOLD_S = 30


class DiagController(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]
    _CONTEXTS = {'wsgi': WSGIApplication}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.dps = {}
        self.switch_ports = {}
        self.graph = nx.Graph()
        self.full_graph = nx.Graph()
        self.link_ports = {}
        self.all_link_ports = {}
        self.hosts = {}
        self.flows = {}
        self.metrics = {}
        self.degraded = {}
        self.port_rate = {}
        self._last_stats = {}
        self.event_log = []
        self.dynamic = True
        kwargs['wsgi'].register(DiagRest, {'app': self})
        hub.spawn(self._monitor)

    # ---------- helpers ----------
    def log_event(self, kind, **info):
        self.event_log.append({'time': time.time(), 'event': kind, **info})
        self.logger.info('%s %s', kind, info)

    @staticmethod
    def pid(path):
        p = path if path <= path[::-1] else path[::-1]
        return '-'.join('s%d' % d for d in p)

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def features_handler(self, ev):
        dp = ev.msg.datapath
        p, o = dp.ofproto_parser, dp.ofproto
        self.dps[dp.id] = dp
        self.add_flow(dp, 0, p.OFPMatch(), [p.OFPActionOutput(o.OFPP_CONTROLLER, o.OFPCML_NO_BUFFER)])

    def add_flow(self, dp, priority, match, actions):
        p, o = dp.ofproto_parser, dp.ofproto
        inst = [p.OFPInstructionActions(o.OFPIT_APPLY_ACTIONS, actions)]
        dp.send_msg(p.OFPFlowMod(datapath=dp, priority=priority, match=match, instructions=inst))

    def delete_flow(self, dp, match):
        p, o = dp.ofproto_parser, dp.ofproto
        dp.send_msg(p.OFPFlowMod(datapath=dp, command=o.OFPFC_DELETE,
                                 out_port=o.OFPP_ANY, out_group=o.OFPG_ANY, match=match))

    def packet_out(self, dp, port, data):
        p, o = dp.ofproto_parser, dp.ofproto
        dp.send_msg(p.OFPPacketOut(datapath=dp, buffer_id=o.OFP_NO_BUFFER, in_port=o.OFPP_CONTROLLER,
                                   actions=[p.OFPActionOutput(port)], data=data))

    # ---------- REQ 5: topology discovery ----------
    @set_ev_cls([event.EventSwitchEnter, event.EventSwitchLeave,
                 event.EventLinkAdd, event.EventLinkDelete])
    def topo_change(self, ev):
        self.rebuild_graph()
        self.log_event(type(ev).__name__)
        self.handle_topology_change()

    def rebuild_graph(self):
        switches, links = get_switch(self, None), get_link(self, None)
        directed = {(l.src.dpid, l.dst.dpid): l.src.port_no for l in links}
        g, ports = nx.Graph(), {}
        g.add_nodes_from(sw.dp.id for sw in switches)
        self.switch_ports = {sw.dp.id: [p.port_no for p in sw.ports
                                        if p.port_no <= ofproto_v1_3.OFPP_MAX] for sw in switches}
        for (a, b), port in directed.items():
            if (b, a) in directed:
                g.add_edge(a, b)
                ports[(a, b)] = port
        self.graph, self.link_ports = g, ports
        self.all_link_ports.update(ports)
        self.full_graph.add_nodes_from(g.nodes)
        self.full_graph.add_edges_from(g.edges)

    def inter_switch_ports(self, dpid):
        return {port for (a, _), port in self.all_link_ports.items() if a == dpid}

    def edge_ports(self, dpid):
        return [p for p in self.switch_ports.get(dpid, []) if p not in self.inter_switch_ports(dpid)]

    def learn_host(self, ip, mac, dpid, port):
        if port in self.inter_switch_ports(dpid):
            return
        new = {'mac': mac, 'dpid': dpid, 'port': port}
        if self.hosts.get(ip) != new:
            self.hosts[ip] = new
            self.log_event('HOST_LEARNED', ip=ip, dpid=dpid, port=port)

    def topology_dict(self):
        return {'switches': sorted(self.graph.nodes),
                'links': [{'src': 's%d' % u, 'dst': 's%d' % v,
                           'src_port': self.link_ports[(u, v)], 'dst_port': self.link_ports[(v, u)],
                           'util': round(self.link_util(u, v), 2)} for u, v in self.graph.edges],
                'hosts': [{'ip': ip, **h} for ip, h in self.hosts.items()]}

    # ---------- packet-in: ARP + IP ----------
    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, ev):
        msg, dp = ev.msg, ev.msg.datapath
        in_port = msg.match['in_port']
        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)
        if eth.ethertype in (ether_types.ETH_TYPE_LLDP, ether_types.ETH_TYPE_IPV6):
            return
        a, ip = pkt.get_protocol(arp.arp), pkt.get_protocol(ipv4.ipv4)
        if a:
            self.learn_host(a.src_ip, eth.src, dp.id, in_port)
            self.handle_arp(dp, in_port, msg.data, a.dst_ip)
        elif ip:
            self.learn_host(ip.src, eth.src, dp.id, in_port)
            self.handle_ip(dp, in_port, msg.data, ip.src, ip.dst)

    def handle_arp(self, dp, in_port, data, dst_ip):
        host = self.hosts.get(dst_ip)
        if host and host['dpid'] in self.dps:
            self.packet_out(self.dps[host['dpid']], host['port'], data)
            return
        for dpid, d in self.dps.items():
            for port in self.edge_ports(dpid):
                if not (dpid == dp.id and port == in_port):
                    self.packet_out(d, port, data)

    # ---------- REQ 4: path analysis and flow installation ----------
    @staticmethod
    def flow_key(ip_a, ip_b):
        return tuple(sorted((ip_a, ip_b)))

    def paths_between(self, key, graph):
        a, b = key
        if a not in self.hosts or b not in self.hosts:
            return []
        s, t = self.hosts[a]['dpid'], self.hosts[b]['dpid']
        if s == t:
            return [[s]]
        if s not in graph or t not in graph:
            return []
        return sorted(nx.all_simple_paths(graph, s, t), key=lambda p: (len(p), p))

    def path_state(self, path):
        if any(not self.graph.has_edge(u, v) for u, v in zip(path, path[1:])):
            return 'FAILED'
        if self.pid(path) in self.degraded:
            return 'DEGRADED'
        if any(self.link_util(u, v) > CONGESTION_UTIL for u, v in zip(path, path[1:])):
            return 'CONGESTED'
        return 'UP'

    def choose_path(self, key):
        cands = self.paths_between(key, self.graph)
        if not cands:
            return None
        pinned = self.flows.get(key, {}).get('pinned')
        if pinned:
            for p in cands:
                if self.pid(p) == pinned:
                    return p
        good = [p for p in cands if self.path_state(p) == 'UP']
        if good:
            return good[0]
        return min(cands, key=lambda p: self.metrics.get(self.pid(p), {}).get('loss_pct') or 0)

    def setup_flow(self, ip_a, ip_b, reason='NEW'):
        key = self.flow_key(ip_a, ip_b)
        rec = self.flows.setdefault(key, {'path': None, 'pinned': None})
        new = self.choose_path(key)
        if new is None:
            self.log_event('NO_PATH', flow=key)
            return None
        old = rec['path']
        if old != new:
            if old:
                self.remove_path_flows(key, old)
            self.install_path(key, new)
            rec['path'] = new
            self.log_event('PATH_INSTALLED', flow=key, path=self.pid(new), reason=reason,
                           old=self.pid(old) if old else None)
        return new

    def next_port(self, pth, i, dst_ip):
        if i == len(pth) - 1:
            return self.hosts[dst_ip]['port']
        return self.link_ports[(pth[i], pth[i + 1])]

    def install_path(self, key, path):
        a, b = key
        for src, dst, pth in ((a, b, path), (b, a, path[::-1])):
            for i, dpid in enumerate(pth):
                dp = self.dps[dpid]
                p = dp.ofproto_parser
                match = p.OFPMatch(eth_type=ether_types.ETH_TYPE_IP, ipv4_src=src, ipv4_dst=dst)
                self.add_flow(dp, 100, match, [p.OFPActionOutput(self.next_port(pth, i, dst))])

    def remove_path_flows(self, key, path):
        a, b = key
        for dpid in path:
            dp = self.dps.get(dpid)
            if not dp:
                continue
            p = dp.ofproto_parser
            for src, dst in ((a, b), (b, a)):
                self.delete_flow(dp, p.OFPMatch(eth_type=ether_types.ETH_TYPE_IP,
                                                ipv4_src=src, ipv4_dst=dst))

    def handle_ip(self, dp, in_port, data, src_ip, dst_ip):
        if src_ip not in self.hosts or dst_ip not in self.hosts:
            return
        path = self.setup_flow(src_ip, dst_ip)
        if not path:
            return
        key = self.flow_key(src_ip, dst_ip)
        pth = path if src_ip == key[0] else path[::-1]
        if dp.id in pth:
            self.packet_out(dp, self.next_port(pth, pth.index(dp.id), dst_ip), data)

    def path_info(self, ip_a, ip_b):
        key = self.flow_key(ip_a, ip_b)
        if key[0] not in self.hosts or key[1] not in self.hosts:
            return {'error': 'unknown host (send some traffic first)'}
        rec = self.flows.get(key, {})
        return {'src': ip_a, 'dst': ip_b,
                'active_path': self.pid(rec['path']) if rec.get('path') else None,
                'pinned': rec.get('pinned'),
                'paths': [{'id': self.pid(p), 'switches': p, 'hops': len(p),
                           'state': self.path_state(p), 'metrics': self.metrics.get(self.pid(p))}
                          for p in self.paths_between(key, self.full_graph)]}

    # ---------- REQ 6: application -> controller feedback ----------
    def handle_report(self, rep):
        pid = rep['path']
        rep['time'] = time.time()
        self.metrics[pid] = rep
        bad = (rep.get('loss_pct') or 0) > LOSS_THRESHOLD_PCT or \
              (rep.get('rtt_avg') or 0) > RTT_THRESHOLD_MS
        if bad:
            self.degraded[pid] = time.time()
            self.log_event('PATH_DEGRADED', path=pid, loss=rep.get('loss_pct'), rtt=rep.get('rtt_avg'))
        else:
            self.degraded.pop(pid, None)
        key = self.flow_key(rep['src'], rep['dst'])
        if self.dynamic and key in self.flows:
            self.setup_flow(*key, reason='REPORT')

    # ---------- REQ 7: failure handling and monitoring ----------
    def handle_topology_change(self):
        for key, rec in list(self.flows.items()):
            old = rec['path']
            failed = bool(old) and any(not self.graph.has_edge(u, v) for u, v in zip(old, old[1:]))
            if failed:
                self.log_event('PATH_FAILED', flow=key, path=self.pid(old))
            if self.dynamic:
                self.setup_flow(*key, reason='LINK_FAILURE' if failed else 'TOPOLOGY_CHANGE')

    def _monitor(self):
        while True:
            for dp in list(self.dps.values()):
                p, o = dp.ofproto_parser, dp.ofproto
                dp.send_msg(p.OFPPortStatsRequest(dp, 0, o.OFPP_ANY))
            now, expired = time.time(), []
            for pid, t in list(self.degraded.items()):
                if now - t > DEGRADE_HOLD_S:
                    del self.degraded[pid]
                    expired.append(pid)
            if expired and self.dynamic:
                for key in list(self.flows):
                    self.setup_flow(*key, reason='DEGRADE_EXPIRED')
            hub.sleep(2)

    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def port_stats_handler(self, ev):
        dpid, now = ev.msg.datapath.id, time.time()
        for st in ev.msg.body:
            key, last = (dpid, st.port_no), self._last_stats.get((dpid, st.port_no))
            if last and now > last['t']:
                dt = now - last['t']
                self.port_rate[key] = {'tx_mbps': (st.tx_bytes - last['tx']) * 8 / dt / 1e6,
                                       'rx_mbps': (st.rx_bytes - last['rx']) * 8 / dt / 1e6,
                                       'tx_dropped': st.tx_dropped}
            self._last_stats[key] = {'t': now, 'tx': st.tx_bytes, 'rx': st.rx_bytes}

    def link_util(self, u, v):
        def one(a, b):
            r = self.port_rate.get((a, self.link_ports.get((a, b))))
            return r['tx_mbps'] / CAPACITY_MBPS if r else 0.0
        return max(one(u, v), one(v, u))

    def stats_dict(self):
        return {'links': [{'link': 's%d-s%d' % (u, v), 'util': round(self.link_util(u, v), 2)}
                          for u, v in self.graph.edges]}

    # ---------- REQ 8: manual path selection ----------
    def pin(self, ip_a, ip_b, path_id):
        key = self.flow_key(ip_a, ip_b)
        rec = self.flows.setdefault(key, {'path': None, 'pinned': None})
        rec['pinned'] = None if path_id in (None, 'auto') else path_id
        self.setup_flow(*key, reason='MANUAL_PIN')
        return self.path_info(ip_a, ip_b)


class DiagRest(ControllerBase):
    def __init__(self, req, link, data, **config):
        super().__init__(req, link, data, **config)
        self.app = data['app']

    def _json(self, obj):
        return Response(content_type='application/json', body=json.dumps(obj).encode())

    @route('topology', '/topology', methods=['GET'])
    def topology(self, req, **kw):
        return self._json(self.app.topology_dict())

    @route('path', '/path', methods=['GET'])
    def path(self, req, **kw):
        return self._json(self.app.path_info(req.params['src'], req.params['dst']))

    @route('report', '/report', methods=['POST'])
    def report(self, req, **kw):
        self.app.handle_report(json.loads(req.body))
        return self._json({'ok': True})

    @route('events', '/events', methods=['GET'])
    def events(self, req, **kw):
        return self._json(self.app.event_log)

    @route('stats', '/stats', methods=['GET'])
    def stats(self, req, **kw):
        return self._json(self.app.stats_dict())

    @route('reroute', '/reroute', methods=['POST'])
    def reroute(self, req, **kw):
        b = json.loads(req.body)
        return self._json(self.app.pin(b['src'], b['dst'], b.get('path', 'auto')))

    @route('mode', '/mode', methods=['POST'])
    def mode(self, req, **kw):
        self.app.dynamic = bool(json.loads(req.body)['dynamic'])
        return self._json({'dynamic': self.app.dynamic})
