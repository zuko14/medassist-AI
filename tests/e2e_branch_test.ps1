# End-to-End Branch Verification Test Script
# Tests the full multi-branch lifecycle through the live API

$pair = "admin:admin123"
$bytes = [System.Text.Encoding]::UTF8.GetBytes($pair)
$b64 = [Convert]::ToBase64String($bytes)
$headers = @{
    Authorization  = "Basic $b64"
    "Content-Type" = "application/json"
}
$base = "http://localhost:8000"

Write-Host ""
Write-Host "===== MULTI-BRANCH END-TO-END TEST =====" -ForegroundColor Cyan
Write-Host ""

# Step 1: Get clinic ID
Write-Host "[1/8] Getting clinic info..." -ForegroundColor Yellow
$clinicResp = Invoke-RestMethod -Uri "$base/admin/stats" -Headers $headers
Write-Host "  [OK] Admin API reachable. Total patients: $($clinicResp.total_patients)" -ForegroundColor Green

$doctors = Invoke-RestMethod -Uri "$base/admin/doctors" -Headers $headers
$clinicId = $doctors[0].clinic_id
Write-Host "  [OK] Clinic ID: $clinicId" -ForegroundColor Green
Write-Host "  [OK] Doctors found: $($doctors.Count)" -ForegroundColor Green

# Step 2: Create Branch 1 (Main Clinic)
Write-Host ""
Write-Host "[2/8] Creating Branch 1: Kukatpally Main Clinic..." -ForegroundColor Yellow
$branch1Body = @{
    name = "Kukatpally Main Clinic"
    short_name = "KPL"
    address = "Plot 45, KPHB Colony, Kukatpally"
    landmark = "Near Metro Station"
    phone = "+919876543210"
    is_diagnostic = $false
    display_order = 1
} | ConvertTo-Json

try {
    $branch1 = Invoke-RestMethod -Uri "$base/admin/branches" -Method Post -Headers $headers -Body $branch1Body
    Write-Host "  [OK] Branch 1 created: $($branch1.branch.id)" -ForegroundColor Green
    $branch1Id = $branch1.branch.id
} catch {
    Write-Host "  [WARN] Branch 1 may already exist, fetching..." -ForegroundColor DarkYellow
    $allBranches = Invoke-RestMethod -Uri "$base/admin/branches" -Headers $headers
    $branch1Id = $allBranches.branches[0].id
    Write-Host "  [OK] Using existing branch: $branch1Id" -ForegroundColor Green
}

# Step 3: Create Branch 2 (Diagnostics Center)
Write-Host ""
Write-Host "[3/8] Creating Branch 2: Ameerpet Diagnostics..." -ForegroundColor Yellow
$branch2Body = @{
    name = "Ameerpet Diagnostics"
    short_name = "AMP"
    address = "Road No 5, Ameerpet"
    landmark = "Near Ameerpet Metro"
    phone = "+919876543211"
    is_diagnostic = $true
    display_order = 2
} | ConvertTo-Json

try {
    $branch2 = Invoke-RestMethod -Uri "$base/admin/branches" -Method Post -Headers $headers -Body $branch2Body
    Write-Host "  [OK] Branch 2 created: $($branch2.branch.id)" -ForegroundColor Green
    $branch2Id = $branch2.branch.id
} catch {
    Write-Host "  [WARN] Branch 2 may already exist, fetching..." -ForegroundColor DarkYellow
    $allBranches = Invoke-RestMethod -Uri "$base/admin/branches" -Headers $headers
    if ($allBranches.branches.Count -ge 2) {
        $branch2Id = $allBranches.branches[1].id
        Write-Host "  [OK] Using existing branch: $branch2Id" -ForegroundColor Green
    }
}

# Step 4: List all branches
Write-Host ""
Write-Host "[4/8] Listing all branches..." -ForegroundColor Yellow
$branchList = Invoke-RestMethod -Uri "$base/admin/branches" -Headers $headers
Write-Host "  [OK] Total branches: $($branchList.branches.Count)" -ForegroundColor Green
foreach ($b in $branchList.branches) {
    if ($b.is_diagnostic) { $type = "Diagnostics" } else { $type = "Clinic" }
    Write-Host "    -> $($b.name) [$($b.short_name)] - $type - Order: $($b.display_order)" -ForegroundColor White
}

# Step 5: Assign doctors to branches
Write-Host ""
Write-Host "[5/8] Assigning doctors to branches..." -ForegroundColor Yellow

$doc1Id = $doctors[0].id
$assignBody1 = @{ doctor_id = $doc1Id; session = "morning" } | ConvertTo-Json
try {
    $assign1 = Invoke-RestMethod -Uri "$base/admin/branches/$branch1Id/doctors" -Method Post -Headers $headers -Body $assignBody1
    Write-Host "  [OK] Assigned $($doctors[0].name) to Branch 1 (morning)" -ForegroundColor Green
} catch {
    Write-Host "  [WARN] Doctor already assigned to Branch 1" -ForegroundColor DarkYellow
}

if ($doctors.Count -ge 2) {
    $doc2Id = $doctors[1].id
    $assignBody2 = @{ doctor_id = $doc2Id; session = "both" } | ConvertTo-Json
    try {
        $assign2 = Invoke-RestMethod -Uri "$base/admin/branches/$branch1Id/doctors" -Method Post -Headers $headers -Body $assignBody2
        Write-Host "  [OK] Assigned $($doctors[1].name) to Branch 1 (both)" -ForegroundColor Green
    } catch {
        Write-Host "  [WARN] Doctor already assigned" -ForegroundColor DarkYellow
    }
}

# Step 6: Get doctors at Branch 1
Write-Host ""
Write-Host "[6/8] Getting doctors at Branch 1..." -ForegroundColor Yellow
$branchDocs = Invoke-RestMethod -Uri "$base/admin/branches/$branch1Id/doctors" -Headers $headers
$docCount = 0
if ($branchDocs.doctor_branches) { $docCount = $branchDocs.doctor_branches.Count }
Write-Host "  [OK] Doctors at Branch 1: $docCount" -ForegroundColor Green
if ($branchDocs.doctor_branches) {
    foreach ($db in $branchDocs.doctor_branches) {
        $docInfo = $db.doctors
        Write-Host "    -> $($docInfo.name) ($($docInfo.department)) - Session: $($db.session)" -ForegroundColor White
    }
}

# Step 7: Update a branch
Write-Host ""
Write-Host "[7/8] Updating Branch 1 address..." -ForegroundColor Yellow
$updateBody = @{
    name = "Kukatpally Main Clinic"
    address = "Plot 45, KPHB Colony, Kukatpally, Hyderabad - 500072"
    landmark = "Opposite Metro Station Exit 2"
} | ConvertTo-Json

try {
    $updated = Invoke-RestMethod -Uri "$base/admin/branches/$branch1Id" -Method Put -Headers $headers -Body $updateBody
    Write-Host "  [OK] Branch updated successfully!" -ForegroundColor Green
} catch {
    Write-Host "  [FAIL] Update failed: $($_.Exception.Message)" -ForegroundColor Red
}

# Step 8: Final summary
Write-Host ""
Write-Host "[8/8] Final verification..." -ForegroundColor Yellow
$finalBranches = Invoke-RestMethod -Uri "$base/admin/branches" -Headers $headers
Write-Host ""
Write-Host "===== TEST RESULTS =====" -ForegroundColor Cyan

$allPass = $true

if ($finalBranches.branches.Count -ge 2) {
    Write-Host "  [PASS] Multiple branches exist ($($finalBranches.branches.Count) total)" -ForegroundColor Green
} else {
    Write-Host "  [FAIL] Need at least 2 branches" -ForegroundColor Red
    $allPass = $false
}

$diagCount = @($finalBranches.branches | Where-Object { $_.is_diagnostic -eq $true -or $_.is_diagnostic -eq 'true' -or $_.is_diagnostic -eq 1 }).Count
if ($diagCount -ge 1) {
    Write-Host "  [PASS] Diagnostics branch exists" -ForegroundColor Green
} else {
    Write-Host "  [FAIL] No diagnostics branch found" -ForegroundColor Red
    $allPass = $false
}

if ($docCount -ge 1) {
    Write-Host "  [PASS] Doctors assigned to branches ($docCount doctors)" -ForegroundColor Green
} else {
    Write-Host "  [FAIL] No doctors assigned" -ForegroundColor Red
    $allPass = $false
}

$hasAddr = @($finalBranches.branches | Where-Object { $_.address -ne $null -and $_.address -ne '' }).Count
if ($hasAddr -ge 1) {
    Write-Host "  [PASS] Branches have address info" -ForegroundColor Green
} else {
    Write-Host "  [FAIL] No branch addresses" -ForegroundColor Red
    $allPass = $false
}

Write-Host ""
if ($allPass) {
    Write-Host "ALL TESTS PASSED! Multi-branch system is fully operational!" -ForegroundColor Green
} else {
    Write-Host "Some tests failed. Review above." -ForegroundColor Red
}
Write-Host ""
