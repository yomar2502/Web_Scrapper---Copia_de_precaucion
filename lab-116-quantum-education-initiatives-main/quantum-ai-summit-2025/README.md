# Quantum-AI-Summit-2025-stats-analysis
This is a statistical analysis of the attendance patterns in the Quantum AI Summit 2025 (December 19-21). Notwithstanding the diverse academic backgrounds of the participants, the data reveals significant engagement trends; consequently, this analysis is useful to identify the target audience for the upcoming Qiskit Fall Fest 2026
The project combines descriptive statistics, hypothesis testing, clustering, and simple predictive modeling to study participant engagement across multiple days of the event.

The project was entirely developed by the author, who also participated in the organization of the event and led the collection, processing, and analysis of the attendance data.

While the immediate objective is to inform outreach and program design decisions for Qiskit Fall Fest 2026, this analysis is intended as a foundational element of a larger research initiative on quantum computing "readiness" in Peru. In conjunction with future data from nationwide outreach campaigns, additional events, and structured training modules delivered through QuantumHub, this work has the potential to evolve into a quantitative readiness study assessing the country’s capacity to adopt and integrate quantum technologies.

## Objectives
- Analyze attendance dynamics across the three days of the event
- Estimate attendance and dropout rates with statistical uncertainty
- Explore differences in engagement between STEM and non-STEM backgrounds
- Identify typical behavioral profiles using unsupervised learning (K-Means)
- Provide interpretable visual summaries to support decision-making for future events as Qiskit Fall Fest 2026

## Methodology
The analysis includes the following components:
- **Descriptive statistics** for attendance across days
- **Chi-square tests** to explore associations between academic background and dropout behavior
- **Logistic regression** to model early dropout probability
- **K-Means clustering** to identify common attendance patterns
- **Radar chart visualization** for qualitative comparison of cluster profiles

## Key Findings

**Incentive-Driven Retention:** Analysis of daily attendance correlations revealed that retention spiked between Day 2 and Day 3. This suggests the certification threshold (minimum 2-day attendance) was a stronger behavioral driver than the content itself.
  
**The "STEM" Myth:** Chi-square testing ($p \approx 0.30$) debunked the assumption that non-technical audiences drop out faster. Both STEM and non-STEM attendees showed statistically identical dropout patterns, indicating that content complexity was not the primary barrier.

**Algorithmic Profiling:** Unsupervised learning (K-Means) identified three distinct attendee personas, including a "Certificate Strategist" cluster that strategically attended only the last two days, and a "Day-1 Explorer" cluster (mostly professionals) that requires a different engagement strategy for 2026.

