import json
import re
from typing import Any, Dict, List, Optional
from .adb import AdbClient

class Diagnoser:
    def __init__(self, client: AdbClient):
        self.client = client
        self.warnings: List[str] = []

    def run(self, third_party_only: bool = True) -> Dict[str, Any]:
        device = self.client.get_device_metadata_with_fallback()
        
        packages = self._get_packages(third_party=third_party_only)
        
        dumpsys_outputs = {
            "batterystats": self._safe_dumpsys(["batterystats", "--charged"]),
            "deviceidle": self._safe_dumpsys(["deviceidle"]),
            "usagestats": self._safe_dumpsys(["usagestats"]),
            "alarm": self._safe_dumpsys(["alarm"]),
            "jobscheduler": self._safe_dumpsys(["jobscheduler"]),
        }
        
        results = []
        for pkg in packages:
            bucket = self._get_standby_bucket(pkg)
            appops = self._get_appops(pkg)
            
            signals = self._parse_signals(pkg, dumpsys_outputs)
            
            rec, reason = self._recommend(bucket, appops, signals)
            
            results.append({
                "package": pkg,
                "standby_bucket": bucket,
                "run_any_in_background": appops,
                "signals": signals,
                "recommendation": rec,
                "reason": reason,
            })
            
        return {
            "device": device,
            "warnings": self.warnings,
            "packages": results,
        }

    def _safe_dumpsys(self, args: List[str]) -> str:
        try:
            return self.client.shell_text(["dumpsys"] + args, check=True)
        except Exception as e:
            self.warnings.append(f"dumpsys {' '.join(args)} failed")
            return ""

    def _get_packages(self, third_party: bool) -> List[str]:
        args = ["pm", "list", "packages"]
        if third_party:
            args.append("-3")
        try:
            out = self.client.shell_text(args, check=True)
            return [line.split(":", 1)[1].strip() for line in out.splitlines() if ":" in line]
        except Exception as e:
            self.warnings.append(f"Failed to list packages")
            return []

    def _get_standby_bucket(self, pkg: str) -> Optional[str]:
        try:
            out = self.client.shell_text(["am", "get-standby-bucket", pkg], check=True)
            out = out.strip()
            if "unknown" in out.lower() or "error" in out.lower():
                return None
            return out
        except Exception:
            return None

    def _get_appops(self, pkg: str) -> Optional[str]:
        try:
            out = self.client.shell_text(["cmd", "appops", "get", pkg, "RUN_ANY_IN_BACKGROUND"], check=True)
            if "ignore" in out.lower():
                return "ignore"
            if "allow" in out.lower():
                return "allow"
            if "deny" in out.lower():
                return "deny"
            if "default" in out.lower():
                return "default"
            return out.strip()
        except Exception:
            return None

    def _has_package_signal(self, pkg: str, dumpsys_output: str) -> bool:
        if not dumpsys_output:
            return False
        pattern = re.compile(rf'(?:^|[^a-zA-Z0-9_.])({re.escape(pkg)})(?:[^a-zA-Z0-9_.]|$)')
        return bool(pattern.search(dumpsys_output))

    def _parse_signals(self, pkg: str, dumpsys: Dict[str, str]) -> Dict[str, Any]:
        alarms_seen = self._has_package_signal(pkg, dumpsys["alarm"]) if dumpsys["alarm"] else None
        jobs_seen = self._has_package_signal(pkg, dumpsys["jobscheduler"]) if dumpsys["jobscheduler"] else None
        wakelocks_seen = self._has_package_signal(pkg, dumpsys["batterystats"]) if dumpsys["batterystats"] else None
        
        last_used_hint = None
        if dumpsys["usagestats"]:
            for line in dumpsys["usagestats"].splitlines():
                if self._has_package_signal(pkg, line) and "lastTimeUsed" in line:
                    m = re.search(r'lastTimeUsed="([^"]+)"', line)
                    if m:
                        last_used_hint = m.group(1)
                    elif "=" in line:
                        parts = line.split()
                        for p in parts:
                            if p.startswith("lastTimeUsed="):
                                last_used_hint = p.split("=")[1].strip('"')
                    break
        
        return {
            "alarms_seen": alarms_seen,
            "jobs_seen": jobs_seen,
            "wakelocks_seen": wakelocks_seen,
            "last_used_hint": last_used_hint
        }
        
    def _recommend(self, bucket: Optional[str], appops: Optional[str], signals: Dict[str, Any]) -> tuple[str, str]:
        if signals.get("wakelocks_seen") and signals.get("alarms_seen") and signals.get("jobs_seen"):
            return "aggressive_restrict", "High background activity (wakelocks, alarms, jobs)"
        if signals.get("alarms_seen") or signals.get("jobs_seen"):
            return "restrict", "Moderate background activity"
        return "keep", "Minimal background activity detected"
