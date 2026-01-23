#!/usr/bin/env python3
"""
5-hour monitoring session for Minus ad detection.
Logs stats every 10 minutes and tracks VLM/OCR detections.
"""

import os
import re
import time
import json
from datetime import datetime, timedelta
from pathlib import Path

LOG_FILE = "/tmp/minus.log"
STATS_FILE = "/tmp/minus_monitor_stats.json"
REPORT_FILE = "/home/radxa/Minus/monitoring_report.md"

# Session configuration
SESSION_DURATION_HOURS = 5
CHECK_INTERVAL_MINUTES = 10

def parse_log_line(line):
    """Parse a log line and extract relevant info."""
    result = {
        'timestamp': None,
        'type': None,
        'is_ad': None,
        'response': None,
        'latency': None,
        'keywords': None,
        'texts': None,
        'blocking_started': False,
        'blocking_ended': False,
        'blocking_duration': None,
        'blocking_source': None,
    }

    # Extract timestamp
    ts_match = re.match(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})', line)
    if ts_match:
        result['timestamp'] = ts_match.group(1)

    # VLM detection
    vlm_match = re.search(r'VLM #(\d+): ([\d.]+)s \[(AD|NO-AD)\] "([^"]*)"', line)
    if vlm_match:
        result['type'] = 'vlm'
        result['latency'] = float(vlm_match.group(2))
        result['is_ad'] = vlm_match.group(3) == 'AD'
        result['response'] = vlm_match.group(4)
        return result

    # OCR detection with blocking
    ocr_block_match = re.search(r'OCR #(\d+): cap=(\d+)ms ocr=(\d+)ms \[BLOCKING (OCR|VLM|OCR\+VLM)\] - (.+)', line)
    if ocr_block_match:
        result['type'] = 'ocr'
        result['latency'] = (int(ocr_block_match.group(2)) + int(ocr_block_match.group(3))) / 1000.0
        result['is_ad'] = True
        result['texts'] = ocr_block_match.group(5)
        return result

    # OCR detection without blocking
    ocr_match = re.search(r'OCR #(\d+): cap=(\d+)ms ocr=(\d+)ms - (.+)', line)
    if ocr_match:
        result['type'] = 'ocr'
        result['latency'] = (int(ocr_match.group(2)) + int(ocr_match.group(3))) / 1000.0
        result['is_ad'] = False
        result['texts'] = ocr_match.group(4)
        return result

    # OCR keywords detected
    keywords_match = re.search(r'OCR detected ad keywords: \[([^\]]+)\]', line)
    if keywords_match:
        result['type'] = 'ocr_keywords'
        result['keywords'] = keywords_match.group(1)
        return result

    # Blocking started
    if 'AD BLOCKING STARTED' in line:
        result['blocking_started'] = True
        source_match = re.search(r'source=([\w+]+)', line)
        if source_match:
            result['blocking_source'] = source_match.group(1)
        return result

    # Blocking ended
    block_end_match = re.search(r'AD BLOCKING ENDED after ([\d.]+)s', line)
    if block_end_match:
        result['blocking_ended'] = True
        result['blocking_duration'] = float(block_end_match.group(1))
        return result

    # Home screen detected
    if 'home screen detected' in line.lower():
        result['type'] = 'home_screen_suppression'
        return result

    return None


def get_log_lines_since(since_time):
    """Get log lines since a given timestamp."""
    lines = []
    try:
        with open(LOG_FILE, 'r') as f:
            for line in f:
                ts_match = re.match(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})', line)
                if ts_match:
                    line_time = datetime.strptime(ts_match.group(1), '%Y-%m-%d %H:%M:%S')
                    if line_time >= since_time:
                        lines.append(line.strip())
    except Exception as e:
        print(f"Error reading log: {e}")
    return lines


def analyze_interval(lines):
    """Analyze log lines and return stats."""
    stats = {
        'vlm_total': 0,
        'vlm_ads': 0,
        'vlm_no_ads': 0,
        'vlm_latencies': [],
        'vlm_responses': [],
        'ocr_total': 0,
        'ocr_ads': 0,
        'ocr_keywords_found': [],
        'ocr_latencies': [],
        'blocking_events': [],
        'home_screen_suppressions': 0,
        'false_positive_candidates': [],  # VLM said ad but OCR didn't
    }

    last_vlm_ad = False
    last_ocr_ad = False

    for line in lines:
        parsed = parse_log_line(line)
        if not parsed:
            continue

        if parsed['type'] == 'vlm':
            stats['vlm_total'] += 1
            if parsed['is_ad']:
                stats['vlm_ads'] += 1
                last_vlm_ad = True
            else:
                stats['vlm_no_ads'] += 1
                last_vlm_ad = False
            stats['vlm_latencies'].append(parsed['latency'])
            if parsed['response'] and parsed['response'] not in ['Yes.', 'No.', 'Yes', 'No']:
                stats['vlm_responses'].append(parsed['response'][:100])

        elif parsed['type'] == 'ocr':
            stats['ocr_total'] += 1
            if parsed['is_ad']:
                stats['ocr_ads'] += 1
                last_ocr_ad = True
            else:
                last_ocr_ad = False
            stats['ocr_latencies'].append(parsed['latency'])

        elif parsed['type'] == 'ocr_keywords':
            stats['ocr_keywords_found'].append(parsed['keywords'])

        elif parsed['type'] == 'home_screen_suppression':
            stats['home_screen_suppressions'] += 1

        elif parsed['blocking_started']:
            stats['blocking_events'].append({
                'action': 'started',
                'source': parsed['blocking_source'],
                'timestamp': parsed['timestamp']
            })

        elif parsed['blocking_ended']:
            stats['blocking_events'].append({
                'action': 'ended',
                'duration': parsed['blocking_duration'],
                'timestamp': parsed['timestamp']
            })

    return stats


def format_stats_report(stats, interval_num, interval_start, interval_end):
    """Format stats into a readable report section."""
    report = []
    report.append(f"\n## Check-in #{interval_num} ({interval_start.strftime('%H:%M')} - {interval_end.strftime('%H:%M')})\n")

    # VLM stats
    vlm_ad_rate = (stats['vlm_ads'] / stats['vlm_total'] * 100) if stats['vlm_total'] > 0 else 0
    vlm_avg_latency = sum(stats['vlm_latencies']) / len(stats['vlm_latencies']) if stats['vlm_latencies'] else 0
    report.append(f"### VLM Stats")
    report.append(f"- Total inferences: {stats['vlm_total']}")
    report.append(f"- Ads detected: {stats['vlm_ads']} ({vlm_ad_rate:.1f}%)")
    report.append(f"- No-ads: {stats['vlm_no_ads']}")
    report.append(f"- Avg latency: {vlm_avg_latency:.2f}s")
    if stats['vlm_responses']:
        report.append(f"- Notable responses: {stats['vlm_responses'][:3]}")
    report.append("")

    # OCR stats
    ocr_ad_rate = (stats['ocr_ads'] / stats['ocr_total'] * 100) if stats['ocr_total'] > 0 else 0
    ocr_avg_latency = sum(stats['ocr_latencies']) / len(stats['ocr_latencies']) if stats['ocr_latencies'] else 0
    report.append(f"### OCR Stats")
    report.append(f"- Total scans: {stats['ocr_total']}")
    report.append(f"- Ads detected: {stats['ocr_ads']} ({ocr_ad_rate:.1f}%)")
    report.append(f"- Avg latency: {ocr_avg_latency:.2f}s")
    if stats['ocr_keywords_found']:
        unique_keywords = list(set(stats['ocr_keywords_found']))[:5]
        report.append(f"- Keywords found: {unique_keywords}")
    report.append("")

    # Blocking events
    report.append(f"### Blocking Events")
    if stats['blocking_events']:
        for event in stats['blocking_events']:
            if event['action'] == 'started':
                report.append(f"- ⏸️ Started at {event['timestamp']} (source: {event['source']})")
            else:
                report.append(f"- ▶️ Ended at {event['timestamp']} (duration: {event['duration']:.1f}s)")
    else:
        report.append("- No blocking events this interval")
    report.append("")

    # Suppressions
    if stats['home_screen_suppressions'] > 0:
        report.append(f"### Suppressions")
        report.append(f"- Home screen suppressions: {stats['home_screen_suppressions']}")
        report.append("")

    return "\n".join(report)


def run_monitoring_session():
    """Run the 5-hour monitoring session."""
    session_start = datetime.now()
    session_end = session_start + timedelta(hours=SESSION_DURATION_HOURS)

    print(f"🎬 Starting {SESSION_DURATION_HOURS}-hour monitoring session")
    print(f"   Start: {session_start.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"   End:   {session_end.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"   Check-ins every {CHECK_INTERVAL_MINUTES} minutes")
    print()

    # Initialize report
    with open(REPORT_FILE, 'w') as f:
        f.write(f"# Minus Ad Detection Monitoring Report\n\n")
        f.write(f"**Session Start:** {session_start.strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"**Session Duration:** {SESSION_DURATION_HOURS} hours\n")
        f.write(f"**Check-in Interval:** {CHECK_INTERVAL_MINUTES} minutes\n\n")
        f.write("---\n")

    # Cumulative stats
    cumulative = {
        'total_vlm_inferences': 0,
        'total_vlm_ads': 0,
        'total_ocr_scans': 0,
        'total_ocr_ads': 0,
        'total_blocking_events': 0,
        'total_blocking_time': 0,
        'all_keywords': [],
    }

    interval_num = 0
    interval_start = session_start

    while datetime.now() < session_end:
        # Wait for the check interval
        time.sleep(CHECK_INTERVAL_MINUTES * 60)

        interval_num += 1
        interval_end = datetime.now()

        print(f"\n📊 Check-in #{interval_num} at {interval_end.strftime('%H:%M:%S')}")

        # Get and analyze logs for this interval
        lines = get_log_lines_since(interval_start)
        stats = analyze_interval(lines)

        # Update cumulative stats
        cumulative['total_vlm_inferences'] += stats['vlm_total']
        cumulative['total_vlm_ads'] += stats['vlm_ads']
        cumulative['total_ocr_scans'] += stats['ocr_total']
        cumulative['total_ocr_ads'] += stats['ocr_ads']
        cumulative['all_keywords'].extend(stats['ocr_keywords_found'])

        for event in stats['blocking_events']:
            if event['action'] == 'started':
                cumulative['total_blocking_events'] += 1
            elif event['action'] == 'ended':
                cumulative['total_blocking_time'] += event.get('duration', 0)

        # Print summary
        print(f"   VLM: {stats['vlm_ads']}/{stats['vlm_total']} ads")
        print(f"   OCR: {stats['ocr_ads']}/{stats['ocr_total']} ads")
        print(f"   Blocking events: {len([e for e in stats['blocking_events'] if e['action'] == 'started'])}")

        # Append to report
        report_section = format_stats_report(stats, interval_num, interval_start, interval_end)
        with open(REPORT_FILE, 'a') as f:
            f.write(report_section)

        # Save cumulative stats
        with open(STATS_FILE, 'w') as f:
            json.dump({
                'session_start': session_start.isoformat(),
                'last_update': interval_end.isoformat(),
                'interval_num': interval_num,
                'cumulative': cumulative,
            }, f, indent=2)

        interval_start = interval_end

    # Final summary
    print(f"\n🏁 Monitoring session complete!")
    print(f"   Total VLM ads: {cumulative['total_vlm_ads']}/{cumulative['total_vlm_inferences']}")
    print(f"   Total OCR ads: {cumulative['total_ocr_ads']}/{cumulative['total_ocr_scans']}")
    print(f"   Total blocking events: {cumulative['total_blocking_events']}")
    print(f"   Total blocking time: {cumulative['total_blocking_time']:.1f}s")

    # Write final summary to report
    with open(REPORT_FILE, 'a') as f:
        f.write("\n---\n\n## Session Summary\n\n")
        f.write(f"- **Total VLM Inferences:** {cumulative['total_vlm_inferences']}\n")
        f.write(f"- **Total VLM Ads Detected:** {cumulative['total_vlm_ads']}\n")
        vlm_rate = (cumulative['total_vlm_ads'] / cumulative['total_vlm_inferences'] * 100) if cumulative['total_vlm_inferences'] > 0 else 0
        f.write(f"- **VLM Ad Rate:** {vlm_rate:.1f}%\n")
        f.write(f"- **Total OCR Scans:** {cumulative['total_ocr_scans']}\n")
        f.write(f"- **Total OCR Ads Detected:** {cumulative['total_ocr_ads']}\n")
        ocr_rate = (cumulative['total_ocr_ads'] / cumulative['total_ocr_scans'] * 100) if cumulative['total_ocr_scans'] > 0 else 0
        f.write(f"- **OCR Ad Rate:** {ocr_rate:.1f}%\n")
        f.write(f"- **Total Blocking Events:** {cumulative['total_blocking_events']}\n")
        f.write(f"- **Total Time Blocked:** {cumulative['total_blocking_time']:.1f}s ({cumulative['total_blocking_time']/60:.1f} min)\n")

        if cumulative['all_keywords']:
            from collections import Counter
            keyword_counts = Counter(cumulative['all_keywords'])
            f.write(f"\n### Top Keywords Detected\n")
            for kw, count in keyword_counts.most_common(10):
                f.write(f"- `{kw}`: {count} times\n")


if __name__ == "__main__":
    run_monitoring_session()
