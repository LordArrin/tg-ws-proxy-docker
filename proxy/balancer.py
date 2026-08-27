import random
from collections import Counter

from typing import Dict, List, Iterator 


class _Balancer:
    def __init__(self):
        self.domains: List[str] = []
        self._dc_to_domain: Dict[int, str] = {}
        self._rr_indices: Dict[int, int] = {}
    
    def update_domains_list(self, domains_list: List[str]) -> None:
        if Counter(self.domains) == Counter(domains_list):
            return
        
        self.domains = domains_list[:]

        self._dc_to_domain = {
            dc_id: random.choice(self.domains)
            for dc_id in (1, 2, 3, 4, 5, 203)
        } if self.domains else {}

    def update_domain_for_dc(self, dc_id: int, domain: str) -> bool:
        if self._dc_to_domain.get(dc_id) == domain:
            return False
        
        self._dc_to_domain[dc_id] = domain
        return True

    def get_domains_for_dc(self, dc_id: int) -> Iterator[str]:
        current_domain = self._dc_to_domain.get(dc_id)
        if current_domain is not None:
            yield current_domain

        if not self.domains:
            return

        idx = self._rr_indices.get(dc_id, 0)
        n = len(self.domains)
        for offset in range(n):
            d = self.domains[(idx + offset) % n]
            if d != current_domain:
                yield d
        self._rr_indices[dc_id] = (idx + 1) % n


balancer = _Balancer()
