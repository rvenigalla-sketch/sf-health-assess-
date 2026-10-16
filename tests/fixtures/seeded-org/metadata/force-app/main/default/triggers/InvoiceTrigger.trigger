trigger InvoiceTrigger on Invoice__c (after insert, after update) {
    InvoiceService.recalculate(Trigger.new);
}
