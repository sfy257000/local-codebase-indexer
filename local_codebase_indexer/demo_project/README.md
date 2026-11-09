# Synthetic order-maintenance sample

This fixture was written for Local Codebase Indexer. It is deliberately small
and is not a production application, an independently collected benchmark, or
a runnable .NET project. No SQL Server or .NET runtime is needed to analyze it.

Scenario: inspect the known static accesses to dbo.Sales_OrderHeader before
changing its Status column. Reads go through OrderRepository.Get; writes go
through usp_ApproveOrder and OrderRepository.Approve. OrderService connects
these operations to OrdersController and Views/Orders/Details.cshtml.

Interfaces make some call paths probable rather than confirmed. Dynamic SQL,
reflection, runtime routes and writes outside the scanned files still need
manual verification. This fixture demonstrates known relationships; it does
not measure the tool's recall on arbitrary applications.
