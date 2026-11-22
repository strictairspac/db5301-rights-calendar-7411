/** @typedef {{id:number, title:string, payload:object, author:string, cleaned_at?:string|null}} BusinessCase */
/** @typedef {{id:number, case_id:number, status:'queued'|'running'|'complete'|'failed'}} Run */
/** @typedef {{id:number, run_id:number, digest:string, reviewer?:string|null, review_note?:string|null, reviewed_at?:string|null, report:{summary:string, metrics:object, sections:Array}}} Evidence */
