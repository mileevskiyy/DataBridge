export type RequestFn=<T>(path:string,method?:string,body?:unknown)=>Promise<T>;
export type Rule={key:string;label:string;type:string;required:boolean;normalization?:string;max_length?:number;minimum?:string|null;maximum?:string|null;scale?:number;allowed_values?:string[]};
export type Schema={id:string;name:string;version:number;versions?:number[];builtin:boolean;definition:{key_field:string;fields:Rule[]}};
export type Options={delimiter:string;decimal_separator:string;date_format:string};
export type Recipe={id:string;version:number;versions?:number[];name:string;schema_id:string;schema_version:number;mapping:Record<string,string>;policy:string;options:Options};
export const defaults:Options={delimiter:',',decimal_separator:'.',date_format:'iso'};
export function emptyRule(key='field'):Rule{return {key,label:key==='sku'?'SKU':key==='name'?'Product name':'Field',type:'text',required:true,normalization:'trim',max_length:200,minimum:null,maximum:null,scale:2,allowed_values:[]};}
